import * as ts from 'typescript';

/**
 * Test-only, AST-based discovery of `useForm` + `zodResolver` form sites (FU-261-1).
 *
 * Each source is parsed with the TypeScript compiler API (the `typescript` package
 * apps/web already pins), so comments, strings and template text can never count as forms.
 * Imports are resolved per file: named imports, import aliases (`useForm as useF`) and
 * namespace imports (`import * as RHF from 'react-hook-form'` → `RHF.useForm`) of
 * `react-hook-form` and `@hookform/resolvers/zod`.
 *
 * A SITE is a direct call of the imported `useForm` whose first argument is an object
 * literal with a `resolver: zodResolver(...)` property, where `zodResolver` is itself a
 * direct call of the imported function. Its label is `file:line` of the `useForm` name
 * token, the form the runtime recorder (`@/test/form-defaults`) reads from the stack.
 *
 * The discoverer is deliberately narrow: every shape it does not understand is returned as
 * a named, site-specific PROBLEM (see `ProblemKind`) instead of being skipped, so the
 * caller fails closed. It holds no list of expected forms.
 *
 * One site label must mean one form (review r1 F1). A function that takes its schema or its
 * defaults from its CALLER is a shared wrapper: every caller would record the same
 * `file:line`, so one exercised caller would cover an unrendered one. Such a site is the
 * problem `parameter-sourced-form-input`, whatever the function is called (component,
 * anonymous default export, class method, `memo(...)` callback, any capitalisation). The
 * rule is syntactic: the `zodResolver(...)` argument or the `defaultValues` value is
 * followed through parentheses, type assertions, `!`, member and element access (`props.s`,
 * `p.s.extend(...)` — a call's receiver, never its arguments), `?:` / `??` / `||` / `&&`
 * branches, object-literal values and spreads, and same-scope variable aliases and
 * destructuring (`const { schema } = props`), and it is a problem when that reaches a
 * parameter (destructured or not), `this` or `arguments`. It does NOT follow a value
 * computed by a function call from its arguments (`useMemo(() => …, [roles])`,
 * `make(props.x)`), context, or module-level mutable state; see the FU-261-1 fix-r1 report.
 *
 * `useForm` / `zodResolver` imported from any other module (review r1 F2: a re-export
 * outside the scanned glob, e.g. under `src/test/` or outside `src/`) is the problem
 * `foreign-form-import`, as is `NS.useForm` / `NS.zodResolver` on a namespace or default
 * import of another module.
 */

export const RHF_MODULE = 'react-hook-form';
export const ZOD_RESOLVER_MODULE = '@hookform/resolvers/zod';

export type ProblemKind =
  /** `import RHF from …` or `import { default as X } from …` of either package. */
  | 'default-import'
  /** A deep or sibling specifier (`react-hook-form/…`, `@hookform/resolvers`, `…/yup`). */
  | 'unsupported-specifier'
  /** `import(…)` of either package, or with a specifier that is not a string literal. */
  | 'dynamic-import'
  /** `require(…)` / `import x = require(…)` of either package, or with a non-literal specifier. */
  | 'require'
  /** `export … from` either package, or an export of an imported `useForm` / `zodResolver` / namespace. */
  | 're-export'
  /** `useForm` / `zodResolver` used other than as the callee of a direct call (alias, argument, …). */
  | 'value-alias'
  /** A namespace import used other than as `NS.member` (destructured, passed, `NS['x']`, …). */
  | 'namespace-escape'
  /** A local declaration reuses the local name of a tracked import. */
  | 'shadowed-binding'
  /** `useForm(x)` where `x` is not an object literal. */
  | 'options-not-literal'
  /** A spread (`...opts`) in the `useForm` options literal. */
  | 'options-spread'
  /** A computed key (`['resolver']`) in the `useForm` options literal. */
  | 'options-computed-key'
  /** `resolver:` present but not a direct `zodResolver(...)` call (variable, lambda, ternary, shorthand, method, …). */
  | 'resolver-not-direct-zod-call'
  /** A `zodResolver(...)` call that is not the `resolver` of a recognised `useForm` options literal. */
  | 'zod-resolver-outside-useform'
  /** A recognised site inside a function named `use…` (a custom wrapper hook). */
  | 'custom-hook-wrapper'
  /**
   * A recognised site whose `zodResolver(...)` schema or `defaultValues` flows from a parameter
   * (or `this` / `arguments`) of an enclosing function: a shared wrapper, whose one site label
   * would stand for every caller (review r1 F1).
   */
  | 'parameter-sourced-form-input'
  /**
   * A binding named `useForm` / `zodResolver` imported from a module other than its package, or
   * read as a member of another module's namespace or default import (review r1 F2).
   */
  | 'foreign-form-import';

export interface DiscoveredSite {
  /** `path/File.tsx:line`, relative to `src/`, as the runtime site label. */
  site: string;
}

export interface DiscoveryProblem {
  site: string;
  kind: ProblemKind;
  detail: string;
}

export interface DiscoveryResult {
  sites: DiscoveredSite[];
  problems: DiscoveryProblem[];
  filesScanned: number;
}

type Tracked = 'useForm' | 'zodResolver';

interface FileBindings {
  /** local identifier → which tracked export it is. */
  locals: Map<string, Tracked>;
  /** local namespace identifier → module it names. */
  namespaces: Map<string, typeof RHF_MODULE | typeof ZOD_RESOLVER_MODULE>;
}

const TRACKED_EXPORT: Record<string, Tracked> = {
  [RHF_MODULE]: 'useForm',
  [ZOD_RESOLVER_MODULE]: 'zodResolver',
};

const TRACKED_NAMES: ReadonlySet<string> = new Set<Tracked>(['useForm', 'zodResolver']);

function isTrackedModule(spec: string): spec is typeof RHF_MODULE | typeof ZOD_RESOLVER_MODULE {
  return spec === RHF_MODULE || spec === ZOD_RESOLVER_MODULE;
}

/** Specifiers of the two package families that are not one of the two exact entry points. */
function isNearMissModule(spec: string): boolean {
  if (isTrackedModule(spec)) return false;
  return spec.startsWith(`${RHF_MODULE}/`) || spec === '@hookform/resolvers' || spec.startsWith('@hookform/resolvers/');
}

function scriptKind(path: string): ts.ScriptKind {
  if (path.endsWith('.tsx')) return ts.ScriptKind.TSX;
  if (path.endsWith('.jsx')) return ts.ScriptKind.JSX;
  if (/\.[cm]?js$/.test(path)) return ts.ScriptKind.JS;
  return ts.ScriptKind.TS;
}

/** True when `node` is the NAME of a property, member, attribute, label or import/export slot — not a value reference. */
function isNamePosition(node: ts.Identifier): boolean {
  const p = node.parent;
  if (ts.isPropertyAccessExpression(p)) return p.name === node;
  if (ts.isQualifiedName(p)) return p.right === node;
  if (
    ts.isPropertyAssignment(p) ||
    ts.isPropertyDeclaration(p) ||
    ts.isPropertySignature(p) ||
    ts.isMethodDeclaration(p) ||
    ts.isMethodSignature(p) ||
    ts.isGetAccessorDeclaration(p) ||
    ts.isSetAccessorDeclaration(p) ||
    ts.isEnumMember(p) ||
    ts.isJsxAttribute(p)
  ) {
    return p.name === node;
  }
  if (ts.isBindingElement(p)) return p.propertyName === node;
  if (ts.isLabeledStatement(p) || ts.isBreakOrContinueStatement(p)) return true;
  if (ts.isImportSpecifier(p) || ts.isImportClause(p) || ts.isNamespaceImport(p)) return true;
  if (ts.isExportSpecifier(p)) {
    // `export { a as b } from 'm'` names another module's export; locals are only referenced without `from`.
    if (p.parent.parent.moduleSpecifier) return true;
    return p.propertyName ? p.name === node : false;
  }
  return false;
}

/** True when `node` is the declared name of a local value (a shadowing declaration). */
function isDeclarationName(node: ts.Identifier): boolean {
  const p = node.parent;
  return (
    ((ts.isVariableDeclaration(p) ||
      ts.isParameter(p) ||
      ts.isFunctionDeclaration(p) ||
      ts.isFunctionExpression(p) ||
      ts.isClassDeclaration(p) ||
      ts.isClassExpression(p) ||
      ts.isEnumDeclaration(p) ||
      ts.isModuleDeclaration(p) ||
      ts.isImportEqualsDeclaration(p)) &&
      p.name === node) ||
    (ts.isBindingElement(p) && p.name === node)
  );
}

function insideTypeQuery(node: ts.Node): boolean {
  for (let n: ts.Node | undefined = node.parent; n; n = n.parent) {
    if (ts.isTypeQueryNode(n)) return true;
    if (ts.isStatement(n)) return false;
  }
  return false;
}

function enclosingFunctionName(node: ts.Node): string | undefined {
  // True while walking up from an anonymous callback through call arguments only
  // (`const useX = memo(forwardRef(() => …))`): the variable then names the callback.
  let wrappedCallback = false;
  for (let n: ts.Node | undefined = node.parent; n; n = n.parent) {
    if (ts.isFunctionDeclaration(n) || ts.isMethodDeclaration(n)) return n.name?.getText();
    if (ts.isFunctionExpression(n) || ts.isArrowFunction(n)) {
      if (ts.isFunctionExpression(n) && n.name) return n.name.text;
      const holder = n.parent;
      if (ts.isVariableDeclaration(holder) && ts.isIdentifier(holder.name)) return holder.name.text;
      if (ts.isPropertyAssignment(holder)) return holder.name.getText();
      // An anonymous callback (e.g. `memo(() => …)`) — keep walking to the named owner.
      wrappedCallback = true;
      continue;
    }
    if (wrappedCallback && ts.isVariableDeclaration(n) && ts.isIdentifier(n.name)) return n.name.text;
    if (!(ts.isCallExpression(n) || ts.isParenthesizedExpression(n) || ts.isAsExpression(n) || ts.isSatisfiesExpression(n))) {
      wrappedCallback = false;
    }
  }
  return undefined;
}

/** True when the binding pattern (or plain name) declares `text`. */
function bindsName(name: ts.BindingName, text: string): boolean {
  if (ts.isIdentifier(name)) return name.text === text;
  return name.elements.some((el) => !ts.isOmittedExpression(el) && bindsName(el.name, text));
}

type Binding = { kind: 'parameter' } | { kind: 'variable'; source: ts.Expression | undefined } | { kind: 'other' };

/**
 * The nearest declaration of the identifier `ref`, found by walking the enclosing scopes:
 * a parameter of an enclosing function, a variable (with the expression its value comes
 * from), or anything else (function, class, enum, catch variable). `undefined` when the
 * name is not declared in this file except by an import (or is a global).
 */
function resolveBinding(ref: ts.Identifier): Binding | undefined {
  const text = ref.text;
  const fromList = (list: ts.VariableDeclarationList, source: (d: ts.VariableDeclaration) => ts.Expression | undefined) => {
    const decl = list.declarations.find((d) => bindsName(d.name, text));
    return decl ? ({ kind: 'variable', source: source(decl) } as const) : undefined;
  };
  for (let n: ts.Node | undefined = ref.parent; n; n = n.parent) {
    if (ts.isFunctionLike(n) && n.parameters.some((p) => bindsName(p.name, text))) return { kind: 'parameter' };
    if ((ts.isForOfStatement(n) || ts.isForInStatement(n)) && ts.isVariableDeclarationList(n.initializer)) {
      const loop = n;
      const found = fromList(n.initializer, () => loop.expression);
      if (found) return found;
    }
    if (ts.isForStatement(n) && n.initializer && ts.isVariableDeclarationList(n.initializer)) {
      const found = fromList(n.initializer, (d) => d.initializer);
      if (found) return found;
    }
    if (ts.isCatchClause(n) && n.variableDeclaration && bindsName(n.variableDeclaration.name, text)) return { kind: 'other' };
    if (ts.isBlock(n) || ts.isSourceFile(n) || ts.isModuleBlock(n) || ts.isCaseClause(n) || ts.isDefaultClause(n)) {
      for (const stmt of n.statements) {
        if (ts.isVariableStatement(stmt)) {
          const found = fromList(stmt.declarationList, (d) => d.initializer);
          if (found) return found;
        }
        if (
          (ts.isFunctionDeclaration(stmt) || ts.isClassDeclaration(stmt) || ts.isEnumDeclaration(stmt)) &&
          stmt.name?.text === text
        ) {
          return { kind: 'other' };
        }
      }
    }
  }
  return undefined;
}

/**
 * When `expr` (a zodResolver schema argument or a `defaultValues` value) flows from a
 * parameter, `this` or `arguments` of an enclosing function, a description of that source;
 * otherwise `undefined`. The flow rules are listed in the header comment.
 */
function parameterSource(expr: ts.Expression | undefined, seen: Set<ts.Node> = new Set()): string | undefined {
  if (!expr || seen.has(expr)) return undefined;
  seen.add(expr);
  let e = expr;
  while (
    ts.isParenthesizedExpression(e) ||
    ts.isAsExpression(e) ||
    ts.isSatisfiesExpression(e) ||
    ts.isNonNullExpression(e) ||
    ts.isTypeAssertionExpression(e)
  ) {
    e = e.expression;
  }
  if (e.kind === ts.SyntaxKind.ThisKeyword) return '`this`';
  if (ts.isIdentifier(e)) {
    const binding = resolveBinding(e);
    if (binding?.kind === 'parameter') return `parameter "${e.text}"`;
    if (binding?.kind === 'variable') {
      const via = parameterSource(binding.source, seen);
      return via && `${via} (via "${e.text}")`;
    }
    if (!binding && e.text === 'arguments') return '`arguments`';
    return undefined;
  }
  if (ts.isPropertyAccessExpression(e) || ts.isElementAccessExpression(e) || ts.isCallExpression(e)) {
    return parameterSource(e.expression, seen);
  }
  if (ts.isConditionalExpression(e)) return parameterSource(e.whenTrue, seen) ?? parameterSource(e.whenFalse, seen);
  if (ts.isBinaryExpression(e)) {
    const op = e.operatorToken.kind;
    if (
      op === ts.SyntaxKind.QuestionQuestionToken ||
      op === ts.SyntaxKind.BarBarToken ||
      op === ts.SyntaxKind.AmpersandAmpersandToken ||
      op === ts.SyntaxKind.CommaToken
    ) {
      return parameterSource(e.left, seen) ?? parameterSource(e.right, seen);
    }
    return undefined;
  }
  if (ts.isObjectLiteralExpression(e)) {
    for (const prop of e.properties) {
      const value = ts.isPropertyAssignment(prop)
        ? prop.initializer
        : ts.isShorthandPropertyAssignment(prop)
          ? prop.name
          : ts.isSpreadAssignment(prop)
            ? prop.expression
            : undefined;
      const found = parameterSource(value, seen);
      if (found) return found;
    }
  }
  return undefined;
}

/** The member name of `X.name` / `X['name']`, when it is static. */
function staticMemberName(node: ts.PropertyAccessExpression | ts.ElementAccessExpression): string | undefined {
  if (ts.isPropertyAccessExpression(node)) return node.name.text;
  return ts.isStringLiteralLike(node.argumentExpression) ? node.argumentExpression.text : undefined;
}

function describeParent(node: ts.Node): string {
  const p = node.parent;
  if (ts.isVariableDeclaration(p)) return 'assigned to a variable';
  if (ts.isCallExpression(p) || ts.isNewExpression(p)) return 'passed as an argument';
  if (ts.isShorthandPropertyAssignment(p) || ts.isPropertyAssignment(p)) return 'stored in an object';
  if (ts.isPropertyAccessExpression(p) || ts.isElementAccessExpression(p)) return 'accessed as an object';
  if (ts.isParenthesizedExpression(p)) return 'wrapped in parentheses';
  if (ts.isBinaryExpression(p)) return 'used in an expression or assignment';
  if (ts.isReturnStatement(p) || ts.isArrowFunction(p)) return 'returned';
  return `used as ${ts.SyntaxKind[p.kind]}`;
}

/** Discover sites and problems in one source. `path` is relative to `src/`, e.g. `pages/auth/SignIn.tsx`. */
export function discoverInSource(path: string, text: string): { sites: DiscoveredSite[]; problems: DiscoveryProblem[] } {
  const sf = ts.createSourceFile(path, text, ts.ScriptTarget.Latest, true, scriptKind(path));
  const at = (node: ts.Node) => `${path}:${sf.getLineAndCharacterOfPosition(node.getStart(sf)).line + 1}`;
  const sites: DiscoveredSite[] = [];
  const problems: DiscoveryProblem[] = [];
  const problem = (node: ts.Node, kind: ProblemKind, detail: string) => problems.push({ site: at(node), kind, detail });

  // Pass 1: import bindings.
  const b: FileBindings = { locals: new Map(), namespaces: new Map() };
  /** Namespace and default imports of every OTHER module: local name → specifier. */
  const foreignObjects = new Map<string, string>();
  for (const stmt of sf.statements) {
    if (!ts.isImportDeclaration(stmt) || !ts.isStringLiteral(stmt.moduleSpecifier)) continue;
    const spec = stmt.moduleSpecifier.text;
    if (isNearMissModule(spec)) {
      problem(stmt, 'unsupported-specifier', `import from "${spec}": only "${RHF_MODULE}" and "${ZOD_RESOLVER_MODULE}" are understood`);
      continue;
    }
    const clause = stmt.importClause;
    if (!isTrackedModule(spec)) {
      // Review r1 F2: another module may re-export the packages from outside the scanned glob.
      if (!clause || clause.isTypeOnly) continue;
      if (clause.name) foreignObjects.set(clause.name.text, spec);
      const named = clause.namedBindings;
      if (named && ts.isNamespaceImport(named)) foreignObjects.set(named.name.text, spec);
      if (named && ts.isNamedImports(named)) {
        for (const el of named.elements) {
          const imported = (el.propertyName ?? el.name).text;
          if (!el.isTypeOnly && TRACKED_NAMES.has(imported)) {
            problem(el, 'foreign-form-import', `"${imported}" imported from "${spec}", not from its package`);
          }
        }
      }
      continue;
    }
    if (!clause || clause.isTypeOnly) continue;
    if (clause.name) problem(clause.name, 'default-import', `default import "${clause.name.text}" of "${spec}"`);
    const named = clause.namedBindings;
    if (!named) continue;
    if (ts.isNamespaceImport(named)) {
      b.namespaces.set(named.name.text, spec);
      continue;
    }
    for (const el of named.elements) {
      if (el.isTypeOnly) continue;
      const imported = (el.propertyName ?? el.name).text;
      if (imported === 'default') {
        problem(el, 'default-import', `"{ default as ${el.name.text} }" from "${spec}"`);
      } else if (imported === TRACKED_EXPORT[spec]) {
        b.locals.set(el.name.text, imported as Tracked);
      } else if (TRACKED_NAMES.has(imported)) {
        problem(el, 'foreign-form-import', `"${imported}" imported from "${spec}", not from its package`);
      }
    }
  }

  const consumedZodCalls = new Set<ts.CallExpression>();
  const zodCalls: ts.CallExpression[] = [];
  const isTrackedRef = (expr: ts.Expression, which: Tracked): boolean => {
    if (ts.isIdentifier(expr)) return b.locals.get(expr.text) === which;
    if (ts.isPropertyAccessExpression(expr) && ts.isIdentifier(expr.expression)) {
      const mod = b.namespaces.get(expr.expression.text);
      return mod !== undefined && TRACKED_EXPORT[mod] === which && expr.name.text === which;
    }
    return false;
  };
  const nameToken = (callee: ts.Expression): ts.Node => (ts.isPropertyAccessExpression(callee) ? callee.name : callee);

  const handleUseFormCall = (call: ts.CallExpression) => {
    const options = call.arguments[0];
    if (!options) return; // useForm() with no options has no resolver: not a zod form.
    if (!ts.isObjectLiteralExpression(options)) {
      problem(call, 'options-not-literal', `useForm options are a ${ts.SyntaxKind[options.kind]}, not an object literal`);
      return;
    }
    let resolver: ts.ObjectLiteralElementLike | undefined;
    let defaults: ts.Expression | undefined;
    let unreadable = false;
    for (const prop of options.properties) {
      if (ts.isSpreadAssignment(prop)) {
        problem(prop, 'options-spread', 'spread in useForm options: the resolver cannot be read');
        unreadable = true;
        continue;
      }
      if (prop.name && ts.isComputedPropertyName(prop.name)) {
        problem(prop, 'options-computed-key', 'computed key in useForm options: the resolver cannot be read');
        unreadable = true;
        continue;
      }
      if (prop.name && (ts.isIdentifier(prop.name) || ts.isStringLiteral(prop.name)) && prop.name.text === 'resolver') {
        resolver = prop;
      }
      if (prop.name && (ts.isIdentifier(prop.name) || ts.isStringLiteral(prop.name)) && prop.name.text === 'defaultValues') {
        if (ts.isPropertyAssignment(prop)) defaults = prop.initializer;
        else if (ts.isShorthandPropertyAssignment(prop)) defaults = prop.name;
      }
    }
    const value = resolver && ts.isPropertyAssignment(resolver) ? resolver.initializer : undefined;
    const direct = value && ts.isCallExpression(value) && isTrackedRef(value.expression, 'zodResolver') ? value : undefined;
    if (unreadable) {
      // Already reported; the resolver call (if any) is part of that report, not a second one.
      if (direct) consumedZodCalls.add(direct);
      return;
    }
    if (!resolver) return; // No resolver: not a zod form.
    if (!direct) {
      problem(
        resolver,
        'resolver-not-direct-zod-call',
        `resolver is ${value ? `a ${ts.SyntaxKind[value.kind]}` : `a ${ts.SyntaxKind[resolver.kind]}`}, not a direct zodResolver(...) call`,
      );
      return;
    }
    consumedZodCalls.add(direct);
    const owner = enclosingFunctionName(call);
    if (owner && /^use[A-Z0-9_]/.test(owner)) {
      problem(nameToken(call.expression), 'custom-hook-wrapper', `useForm + zodResolver inside custom hook "${owner}"`);
      return;
    }
    // Review r1 F1: one site label must stand for one form, not for every caller of a wrapper.
    const fromSchema = parameterSource(direct.arguments[0]);
    const fromDefaults = fromSchema ? undefined : parameterSource(defaults);
    if (fromSchema || fromDefaults) {
      problem(
        nameToken(call.expression),
        'parameter-sourced-form-input',
        `${fromSchema ? 'zodResolver schema' : 'defaultValues'} comes from ${fromSchema ?? fromDefaults} of ` +
          `${owner ? `"${owner}"` : 'an anonymous function'}: a shared wrapper, so one site label would cover every caller`,
      );
      return;
    }
    sites.push({ site: at(nameToken(call.expression)) });
  };

  /** A reference to tracked `which`: either `expr` is a direct callee, or it is a problem. */
  const handleRef = (expr: ts.Expression, which: Tracked) => {
    if (insideTypeQuery(expr)) return; // `typeof useForm` is a type, not a call or an alias.
    const p = expr.parent;
    if (ts.isCallExpression(p) && p.expression === expr) {
      if (which === 'useForm') handleUseFormCall(p);
      else zodCalls.push(p);
      return;
    }
    if (ts.isExportSpecifier(p) || ts.isExportAssignment(p)) {
      problem(expr, 're-export', `${which} is exported from this module`);
      return;
    }
    problem(expr, 'value-alias', `${which} ${describeParent(expr)} instead of being called directly`);
  };

  const isPackageSpecifier = (arg: ts.Expression | undefined) =>
    !arg || !ts.isStringLiteralLike(arg) || isTrackedModule(arg.text) || isNearMissModule(arg.text);

  // Pass 2: every node.
  const visit = (node: ts.Node): void => {
    if (ts.isExportDeclaration(node) && node.moduleSpecifier && ts.isStringLiteral(node.moduleSpecifier)) {
      const spec = node.moduleSpecifier.text;
      if (isTrackedModule(spec) || isNearMissModule(spec)) problem(node, 're-export', `re-export from "${spec}"`);
    }
    if (ts.isImportEqualsDeclaration(node) && ts.isExternalModuleReference(node.moduleReference)) {
      if (isPackageSpecifier(node.moduleReference.expression)) {
        problem(node, 'require', `import ${node.name.text} = require(${node.moduleReference.expression.getText(sf)})`);
      }
    }
    if (ts.isCallExpression(node)) {
      if (node.expression.kind === ts.SyntaxKind.ImportKeyword && isPackageSpecifier(node.arguments[0])) {
        problem(node, 'dynamic-import', `import(${node.arguments[0]?.getText(sf) ?? ''})`);
      }
      if (ts.isIdentifier(node.expression) && node.expression.text === 'require' && isPackageSpecifier(node.arguments[0])) {
        problem(node, 'require', `require(${node.arguments[0]?.getText(sf) ?? ''})`);
      }
    }
    if (
      (ts.isPropertyAccessExpression(node) || ts.isElementAccessExpression(node)) &&
      ts.isIdentifier(node.expression) &&
      foreignObjects.has(node.expression.text)
    ) {
      const member = staticMemberName(node);
      if (member !== undefined && TRACKED_NAMES.has(member)) {
        problem(node, 'foreign-form-import', `"${member}" read from "${foreignObjects.get(node.expression.text)}", not from its package`);
      }
    }
    if (
      ts.isVariableDeclaration(node) &&
      ts.isObjectBindingPattern(node.name) &&
      node.initializer &&
      ts.isIdentifier(node.initializer) &&
      foreignObjects.has(node.initializer.text)
    ) {
      for (const el of node.name.elements) {
        const key = el.propertyName ?? el.name;
        const member = ts.isIdentifier(key) || ts.isStringLiteral(key) ? key.text : undefined;
        if (member !== undefined && TRACKED_NAMES.has(member)) {
          problem(el, 'foreign-form-import', `"${member}" destructured from "${foreignObjects.get(node.initializer.text)}", not from its package`);
        }
      }
    }
    if (ts.isIdentifier(node)) {
      const which = b.locals.get(node.text);
      const ns = b.namespaces.get(node.text);
      if ((which || ns) && !isNamePosition(node)) {
        if (isDeclarationName(node)) {
          problem(node, 'shadowed-binding', `local declaration "${node.text}" shadows the import from "${ns ?? (which === 'useForm' ? RHF_MODULE : ZOD_RESOLVER_MODULE)}"`);
        } else if (which) {
          handleRef(node, which);
        } else if (ns) {
          const p = node.parent;
          if (ts.isQualifiedName(p) && p.left === node) {
            // `RHF.UseFormReturn` in a type position.
          } else if (ts.isPropertyAccessExpression(p) && p.expression === node) {
            const tracked = TRACKED_EXPORT[ns];
            if (tracked && p.name.text === tracked) handleRef(p, tracked);
          } else if (ts.isExportSpecifier(p) || ts.isExportAssignment(p)) {
            problem(node, 're-export', `namespace "${node.text}" of "${ns}" is exported from this module`);
          } else if (!insideTypeQuery(node)) {
            problem(node, 'namespace-escape', `namespace "${node.text}" of "${ns}" ${describeParent(node)}`);
          }
        }
      }
    }
    ts.forEachChild(node, visit);
  };
  visit(sf);

  for (const call of zodCalls) {
    if (!consumedZodCalls.has(call)) {
      problem(call, 'zod-resolver-outside-useform', 'zodResolver(...) is not the resolver of a recognised useForm(...) options literal');
    }
  }
  return { sites, problems };
}

/** Discover across sources keyed by their `src/`-relative path. */
export function discoverFormSites(sources: Record<string, string>): DiscoveryResult {
  const sites: DiscoveredSite[] = [];
  const problems: DiscoveryProblem[] = [];
  const paths = Object.keys(sources).sort();
  for (const path of paths) {
    const found = discoverInSource(path, sources[path]!);
    sites.push(...found.sites);
    problems.push(...found.problems);
  }
  return { sites, problems, filesScanned: paths.length };
}

export function formatProblem(p: DiscoveryProblem): string {
  return `${p.site} [${p.kind}] ${p.detail}`;
}
