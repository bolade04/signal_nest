#!/usr/bin/env python3
"""Certificate-backed completeness providers — Gate 4N-I28BH-B0a golden consumer (gate §17).

WHAT THIS MODULE IS. The reference pattern every certificate-backed completeness consumer
follows. For ONE security-critical collection it supplies the two callables the completeness
framework needs to CERTIFY that the collection is complete, WITHOUT ever reading the collection
itself:

    * a P6 CHANNEL PRODUCER  — derives the authoritative domain INDEPENDENTLY of the collection
      (here from the TRANSITIONS graph, a different code object than the STATES tuple), which the
      framework EXECUTES at registration and injects into the witness. It is what makes the
      "independent authority" a second derivation rather than a second copy of the list.
    * a WITNESS  — OBSERVES the framework-injected authority and returns the observed domain. It
      reads ONLY `payload["_witness_inputs"]` (the injected channel content); it never imports the
      module under test, never reads a module global, and never names the collection — so P2's
      computational-independence perturbation can enumerate and perturb its one dependency and the
      framework can prove, by execution, that the observation is invariant to the collection and
      dependent on the injected authority.

The target collection is reviewer_retrieval_state.py::STATES — the closed universe of reviewer
lane retrieval states.

WHY THE CALLABLES ARE PINNED. Each is pinned by the sha256 of its compiled code object in
completeness_framework.WITNESS_PROVIDER_MANIFEST; editing either body invalidates the pin and
forces a re-review. That is the trust boundary — the framework's signed properties are
pin-CONTENT-independent, so registering a reviewed provider is DATA, not a change to the TCB.

STATIC-RESOLVABILITY. Both functions are plain module-level defs with exactly one parameter, no
closure cells, no decorators, no classes, no dynamic dispatch — so site_taxonomy resolves every
edge and this module introduces no unresolved call.
"""
from __future__ import annotations


def states_authority(source_id):
    """P6 channel producer: derive the reviewer-retrieval state universe INDEPENDENTLY of STATES.

    Reads the TRANSITIONS graph — every source state and every reachable target — which is a
    different code object than the STATES tuple, so this is a genuine second derivation of the
    universe rather than a copy of the collection under test. `source_id` is the channel/source
    name the framework passes in; it is never the collection. Returns a set.
    """
    import reviewer_retrieval_state as retrieval_state
    universe = set()
    for source, targets in retrieval_state.TRANSITIONS.items():
        universe.add(source)
        universe.update(targets)
    return universe


def states_witness(payload):
    """Witness: OBSERVE the framework-injected authority and return the observed domain.

    Reads ONLY the framework-injected P6 channel content under `payload["_witness_inputs"]` — never
    a module global (an unperturbable ambient channel P2 refuses) and never the collection. The
    spec declares exactly one authority channel, so union every injected channel's members; the
    witness never names the channel id and never fetches its own authority. Returns a set.
    """
    observed = set()
    for members in payload["_witness_inputs"].values():
        observed.update(members)
    return observed


# =============================================================================================
# WAVE 2 CONSUMERS (Gate 4N-I28BH-B0a, §34 wave2) — three more certificate-backed collections.
# Same shape as the golden consumer: for each SECURITY_CRITICAL collection, a P6 producer derives
# the authoritative domain from a source that is a DIFFERENT code object than the collection under
# test (non-circular), and a witness observes ONLY the framework-injected authority. Every callable
# is a class-free, single-parameter, statically-resolvable module-level def, and every witness is
# byte-pinned in completeness_framework.WITNESS_PROVIDER_MANIFEST.
# =============================================================================================


def production_states_authority(source_id):
    """P6 channel producer: derive the production-certification state universe INDEPENDENTLY of STATES.

    Reads REQUIRED_FLAG — the dict that maps every certification state to its mandatory
    certifies_production flag. Its KEYS are the states the flag policy governs, authored as a
    separate code object than the STATES tuple, so this is a genuine second derivation of the
    universe rather than a copy of the collection under test. `source_id` is the channel/source
    name the framework passes in; it is never the collection. Returns a set.
    """
    import production_certification as certification
    universe = set()
    for state in certification.REQUIRED_FLAG:
        universe.add(state)
    return universe


def production_states_witness(payload):
    """Witness: OBSERVE the framework-injected authority and return the observed domain.

    Reads ONLY the framework-injected P6 channel content under `payload["_witness_inputs"]` — never
    a module global and never the collection. Unions every injected channel's members. Returns a set.
    """
    observed = set()
    for members in payload["_witness_inputs"].values():
        observed.update(members)
    return observed


def authorization_fields_authority(source_id):
    """P6 channel producer: derive the authorization-field domain INDEPENDENTLY of the constant.

    Reads the independently authored external-authorization contract fixture (via
    production_certification.authorization_contract(), the same tracked file the module itself
    treats as its authority over external expectations) and returns its declared required_fields.
    That fixture is a different artifact than the VALIDATED_AUTHORIZATION_FIELDS tuple, so this is a
    genuine second derivation. `source_id` is the channel/source name; it is never the collection.
    Returns a set.
    """
    import production_certification as certification
    contract = certification.authorization_contract()
    required = set()
    for field in contract["required_fields"]:
        required.add(field)
    return required


def authorization_fields_witness(payload):
    """Witness: OBSERVE the framework-injected authority and return the observed domain.

    Reads ONLY the framework-injected P6 channel content under `payload["_witness_inputs"]` — never
    a module global and never the collection. Unions every injected channel's members. Returns a set.
    """
    observed = set()
    for members in payload["_witness_inputs"].values():
        observed.update(members)
    return observed


def never_relaunch_authority(source_id):
    """P6 channel producer: derive the never-relaunch state set INDEPENDENTLY of NEVER_RELAUNCH.

    Reads the TRANSITIONS graph and derives, from its STRUCTURE, the states that must never be
    automatically relaunched: a state with no inbound edge (the root — a lane still running, not yet
    terminated) or a state with no outbound edge (the sink — a lane that completed with a preserved
    verdict). The remaining, interior states (both an inbound and an outbound edge) are exactly the
    terminated-but-verdict-not-preserved states that may still transition to the sink and so may be
    relaunched. This reads a different code object (TRANSITIONS) than the NEVER_RELAUNCH tuple, so it
    is a genuine second derivation. `source_id` is the channel/source name; never the collection.
    Returns a set.
    """
    import reviewer_retrieval_state as retrieval_state
    graph = retrieval_state.TRANSITIONS
    universe = set()
    for source, targets in graph.items():
        universe.add(source)
        universe.update(targets)
    never = set()
    for state in universe:
        outgoing = graph[state] if state in graph else ()
        incoming = 0
        for other, targets in graph.items():
            if state in targets:
                incoming = incoming + 1
        if len(outgoing) == 0 or incoming == 0:
            never.add(state)
    return never


def never_relaunch_witness(payload):
    """Witness: OBSERVE the framework-injected authority and return the observed domain.

    Reads ONLY the framework-injected P6 channel content under `payload["_witness_inputs"]` — never
    a module global and never the collection. Unions every injected channel's members. Returns a set.
    """
    observed = set()
    for members in payload["_witness_inputs"].values():
        observed.update(members)
    return observed


# =============================================================================================
# WAVE 3 CONSUMERS (Gate 4N-I28BH-B0a, §34 wave3) — three more certificate-backed collections,
# each whose authority is a DIFFERENT code object than the collection under test (non-circular).
# Same shape/constraints as the golden + wave-2 consumers: class-free, single-parameter, statically
# resolvable; every witness is byte-pinned in completeness_framework.WITNESS_PROVIDER_MANIFEST.
# =============================================================================================


def date_operators_authority(source_id):
    """P6 channel producer: derive the Date-operator set INDEPENDENTLY of DATE_OPERATORS.

    Reads SUPPORTED_SEMANTICS['condition_operators'] — the independently authored table of the IAM
    condition operators this evaluator models — and returns the Date* operators declared there. That
    table is a different code object than the DATE_OPERATORS dict, so this is a genuine second
    derivation of the Date-operator universe rather than a copy of the collection under test.
    `source_id` is the channel/source name the framework passes in; never the collection. Returns a set.
    """
    import iam_eval
    operators = set()
    for operator in iam_eval.SUPPORTED_SEMANTICS["condition_operators"]:
        if operator.startswith("Date"):
            operators.add(operator)
    return operators


def date_operators_witness(payload):
    """Witness: OBSERVE the framework-injected authority and return the observed domain.

    Reads ONLY the framework-injected P6 channel content under `payload["_witness_inputs"]` — never
    a module global and never the collection. Unions every injected channel's members. Returns a set.
    """
    observed = set()
    for members in payload["_witness_inputs"].values():
        observed.update(members)
    return observed


def reader_role_authority(source_id):
    """P6 channel producer: derive the revision-reader role set INDEPENDENTLY of ROLE_TRUST.

    Reads signalnest_identity.REVISION_READER_ROLE_NAMES — the authoritative set of reader role
    names created elsewhere in the system — from a DIFFERENT module than trust_policies, so this is
    a genuine second derivation rather than a copy of the ROLE_TRUST keys. `source_id` is the
    channel/source name the framework passes in; it is never the collection. Returns a set.
    """
    import signalnest_identity
    roles = set()
    for role in signalnest_identity.REVISION_READER_ROLE_NAMES:
        roles.add(role)
    return roles


def reader_role_witness(payload):
    """Witness: OBSERVE the framework-injected authority and return the observed domain.

    Reads ONLY the framework-injected P6 channel content under `payload["_witness_inputs"]` — never
    a module global and never the collection. Unions every injected channel's members. Returns a set.
    """
    observed = set()
    for members in payload["_witness_inputs"].values():
        observed.update(members)
    return observed


def assurance_roles_authority(source_id):
    """P6 channel producer: derive the assurance-role set INDEPENDENTLY of _ASSURANCE_ROLES.

    Reads the VALUES of _ASSURANCE_ROLE_BY_MODE — the mode->role map that names, for every workflow
    assurance mode, the role that must verify it. Its value set is the roles the graph validator
    actually dispatches on, mechanically derived from a different code object than the _ASSURANCE_ROLES
    tuple, so this is a genuine second derivation rather than a copy of the collection under test.
    `source_id` is the channel/source name the framework passes in; never the collection. Returns a set.
    """
    import workflow_graph_validator
    roles = set()
    for role in workflow_graph_validator._ASSURANCE_ROLE_BY_MODE.values():
        roles.add(role)
    return roles


def assurance_roles_witness(payload):
    """Witness: OBSERVE the framework-injected authority and return the observed domain.

    Reads ONLY the framework-injected P6 channel content under `payload["_witness_inputs"]` — never
    a module global and never the collection. Unions every injected channel's members. Returns a set.
    """
    observed = set()
    for members in payload["_witness_inputs"].values():
        observed.update(members)
    return observed


# =============================================================================================
# WAVE 4 CONSUMERS (Gate 4N-I28BH-B0a, §34 wave4) — three more certificate-backed collections,
# each a REQUIRED-FIELDS constant whose authority is the KEYSET a cheap, pure in-repo builder
# actually emits (function_result_keys) — a DIFFERENT code object than the constant under test.
# Same shape/constraints as prior waves: class-free, single-parameter, statically resolvable; every
# witness is byte-pinned in completeness_framework.WITNESS_PROVIDER_MANIFEST. The producers execute
# builders that touch NO git/docker/network (pure dict assembly), so running them at registration is
# safe in every environment.
# =============================================================================================


def review_packet_fields_authority(source_id):
    """P6 channel producer: derive the required review-packet digest fields INDEPENDENTLY of REQUIRED_FIELDS.

    Executes review_packet_digest.digests() over a throwaway packet and returns the keys it emits
    EXCEPT the informational `raw_file_bytes` byte-count. digests() is a pure hash-and-assemble
    function whose emitted keyset is a different code object than the REQUIRED_FIELDS tuple, so this
    is a genuine second derivation of the security-enforced field set rather than a copy of the
    collection under test. `source_id` is the channel/source name; never the collection. Returns a set.
    """
    import review_packet_digest
    emitted = review_packet_digest.digests({"completeness_probe": True})
    fields = set()
    for key in emitted:
        if key != "raw_file_bytes":
            fields.add(key)
    return fields


def review_packet_fields_witness(payload):
    """Witness: OBSERVE the framework-injected authority and return the observed domain.

    Reads ONLY the framework-injected P6 channel content under `payload["_witness_inputs"]` — never
    a module global and never the collection. Unions every injected channel's members. Returns a set.
    """
    observed = set()
    for members in payload["_witness_inputs"].values():
        observed.update(members)
    return observed


def generated_arn_keys_authority(source_id):
    """P6 channel producer: derive the generated-resource key set INDEPENDENTLY of GENERATED_KEYS.

    Executes resource_oracle.generated_arns() and returns the keys it produces. generated_arns()
    builds one entry per resource that the policy generators actually emit an ARN for (reading
    gen_operator_policies / gen_boundary_policy), so its keyset is a different code object than the
    GENERATED_KEYS tuple and is a genuine second derivation of the covered-resource universe rather
    than a copy of the collection. `source_id` is the channel/source name; never the collection.
    Returns a set.
    """
    import resource_oracle
    generated = resource_oracle.generated_arns()
    keys = set()
    for key in generated:
        keys.add(key)
    return keys


def generated_arn_keys_witness(payload):
    """Witness: OBSERVE the framework-injected authority and return the observed domain.

    Reads ONLY the framework-injected P6 channel content under `payload["_witness_inputs"]` — never
    a module global and never the collection. Unions every injected channel's members. Returns a set.
    """
    observed = set()
    for members in payload["_witness_inputs"].values():
        observed.update(members)
    return observed


def provenance_fields_authority(source_id):
    """P6 channel producer: derive the required provenance fields INDEPENDENTLY of _PROVENANCE_FIELDS.

    Executes docker_assurance_state._provenance() over throwaway origin/staged_tree strings and
    returns the keys it emits. _provenance() is a pure dict-assembly function (no git/docker); its
    emitted keyset is a different code object than the _PROVENANCE_FIELDS tuple, so this is a genuine
    second derivation of the provenance dimension set rather than a copy of the collection under test.
    `source_id` is the channel/source name; never the collection. Returns a set.
    """
    import docker_assurance_state
    record = docker_assurance_state._provenance("completeness-probe", "completeness-probe")
    fields = set()
    for key in record:
        fields.add(key)
    return fields


def provenance_fields_witness(payload):
    """Witness: OBSERVE the framework-injected authority and return the observed domain.

    Reads ONLY the framework-injected P6 channel content under `payload["_witness_inputs"]` — never
    a module global and never the collection. Unions every injected channel's members. Returns a set.
    """
    observed = set()
    for members in payload["_witness_inputs"].values():
        observed.update(members)
    return observed


# =============================================================================================
# WAVE 5 CONSUMERS (Gate 4N-I28BH-B-SLICE3 shard-a, F2 family — LITERAL-KEY builders) — four more
# certificate-backed REQUIRED-FIELDS constants in workflow_assurance.py. For each, the P6 producer
# executes a workflow_assurance record builder that assembles its record with LITERAL key strings —
# a DIFFERENT code object than the constant under test — and returns the emitted keyset. Each builder
# is PURE: it assembles a dict from throwaway probe inputs and touches NO git/docker/network and does
# NOT derive the Docker state (the establish/pre_build/fresh_docker_state builders, which do, are
# deliberately excluded — see wave5.md skips), so running it at registration is environment-safe.
# Same shape/constraints as prior waves: class-free, single-parameter, statically resolvable; every
# witness is byte-pinned in completeness_framework.WITNESS_PROVIDER_MANIFEST.
# =============================================================================================


def workflow_authorization_fields_authority(source_id):
    """P6 channel producer: derive the workflow authorization field set INDEPENDENTLY of _AUTHORIZATION_FIELDS.

    Executes workflow_assurance._authorization_identity() — a pure, offline builder that binds the
    active issuance/expiry pair from the reviewed constants and assembles its record with LITERAL
    keys ("issuance", "expiry", "duration_seconds", "pair_digest") — and returns that emitted keyset.
    The builder never reads the _AUTHORIZATION_FIELDS tuple, so its keyset is a different code object
    than the collection under test: a genuine second derivation, not a copy. `source_id` is the
    channel/source name the framework passes in; it is never the collection. Returns a set.
    """
    import workflow_assurance
    record = workflow_assurance._authorization_identity()
    fields = set()
    for key in record:
        fields.add(key)
    return fields


def workflow_authorization_fields_witness(payload):
    """Witness: OBSERVE the framework-injected authority and return the observed domain.

    Reads ONLY the framework-injected P6 channel content under `payload["_witness_inputs"]` — never
    a module global and never the collection. Unions every injected channel's members. Returns a set.
    """
    observed = set()
    for members in payload["_witness_inputs"].values():
        observed.update(members)
    return observed


def image_manifest_fields_authority(source_id):
    """P6 channel producer: derive the executed-image manifest field set INDEPENDENTLY of _IMAGE_MANIFEST_FIELDS.

    Executes workflow_assurance.post_build_image_bind() over throwaway probe inputs (an empty
    pre-build record, empty build metadata, no tags) and returns the manifest keys it emits EXCEPT
    the informational `_problems` list. post_build_image_bind assembles the manifest with LITERAL
    keys and is pure (validate/digest/thaw only — no git/docker/network); it emits its full literal
    keyset regardless of whether the probe inputs validate. That keyset is a different code object
    than the _IMAGE_MANIFEST_FIELDS tuple, so this is a genuine second derivation of the manifest
    schema rather than a copy of the collection. `source_id` is never the collection. Returns a set.
    """
    import workflow_assurance
    manifest = workflow_assurance.post_build_image_bind(
        pre_build_record={}, build_metadata={}, resolved_tags=[])
    fields = set()
    for key in manifest:
        if key != "_problems":
            fields.add(key)
    return fields


def image_manifest_fields_witness(payload):
    """Witness: OBSERVE the framework-injected authority and return the observed domain.

    Reads ONLY the framework-injected P6 channel content under `payload["_witness_inputs"]` — never
    a module global and never the collection. Unions every injected channel's members. Returns a set.
    """
    observed = set()
    for members in payload["_witness_inputs"].values():
        observed.update(members)
    return observed


def build_output_fields_authority(source_id):
    """P6 channel producer: derive the build-output field set INDEPENDENTLY of _BUILD_OUTPUT_FIELDS.

    Executes workflow_assurance.post_build_image_bind() over throwaway probe inputs and returns the
    keys of the `build_output` sub-record it assembles. That sub-record is built with LITERAL keys
    ("image_digest", "image_digests", "build_metadata_digest", "builder_result_identity",
    "provenance_source_identity", "resolved_tags") and the builder is pure (no git/docker/network),
    so its keyset is a different code object than the _BUILD_OUTPUT_FIELDS tuple: a genuine second
    derivation rather than a copy of the collection. `source_id` is never the collection. Returns a set.
    """
    import workflow_assurance
    manifest = workflow_assurance.post_build_image_bind(
        pre_build_record={}, build_metadata={}, resolved_tags=[])
    fields = set()
    for key in manifest["build_output"]:
        fields.add(key)
    return fields


def build_output_fields_witness(payload):
    """Witness: OBSERVE the framework-injected authority and return the observed domain.

    Reads ONLY the framework-injected P6 channel content under `payload["_witness_inputs"]` — never
    a module global and never the collection. Unions every injected channel's members. Returns a set.
    """
    observed = set()
    for members in payload["_witness_inputs"].values():
        observed.update(members)
    return observed


def pre_push_fields_authority(source_id):
    """P6 channel producer: derive the pre-push record field set INDEPENDENTLY of _PRE_PUSH_FIELDS.

    Executes workflow_assurance.pre_push_verify() over throwaway probe inputs (empty manifest, empty
    source manifest, empty workflow, empty digest/tags) and returns the keys of the record it emits.
    pre_push_verify assembles the record with LITERAL keys and is pure (validate/digest/authorization
    -identity only — no git/docker/network); it emits its full literal keyset regardless of whether
    the probe inputs validate. That keyset is a different code object than the _PRE_PUSH_FIELDS tuple,
    so this is a genuine second derivation of the pre-push schema rather than a copy of the collection.
    `source_id` is the channel/source name; it is never the collection. Returns a set.
    """
    import workflow_assurance
    record = workflow_assurance.pre_push_verify(
        image_manifest={}, fresh_source_manifest={}, workflow={},
        intended_image_digest="", intended_tags=[],
        fresh_commit_sha="", fresh_tree_identity="")
    fields = set()
    for key in record:
        fields.add(key)
    return fields


def pre_push_fields_witness(payload):
    """Witness: OBSERVE the framework-injected authority and return the observed domain.

    Reads ONLY the framework-injected P6 channel content under `payload["_witness_inputs"]` — never
    a module global and never the collection. Unions every injected channel's members. Returns a set.
    """
    observed = set()
    for members in payload["_witness_inputs"].values():
        observed.update(members)
    return observed


# =============================================================================================
# WAVE 6 CONSUMERS (Gate 4N-I28BH-B-SLICE3 shard-b, F3 family — AST SELF-NAMING CONSTANT
# VOCABULARY) — four more certificate-backed collections. Each collection is a closed vocabulary
# whose members are declared, one per line, as SELF-NAMING module-level constants NAME = "NAME"
# (a structurally-DISTINCT second encoding than the tuple/frozenset that aggregates them). The
# authority is those per-member constants, AST-extracted from the module's OWN source TEXT — a
# different code object (indeed a different representation entirely) than the collection under
# test. A newly-declared member constant that is omitted from the aggregate is caught as
# REQUIRED_SUPERSET drift. Per the F3 soundness adjudication these four modules each contain
# EXACTLY ONE isolable self-naming group whose extracted set equals the collection exactly, so the
# authority is neither self-attesting nor a subset/partition. Same shape/constraints as the golden
# consumer: class-free, single-parameter, statically resolvable; every witness is byte-pinned in
# completeness_framework.WITNESS_PROVIDER_MANIFEST.
#
# NEW INFRASTRUCTURE — the AST authority helper `_ast_self_naming_constants`. It is a plain
# module-level def with exactly one parameter, no closures/decorators/classes, and NO dynamic
# dispatch (no dict-of-extractors, no getattr, no polymorphic visitor): it walks tree.body with
# isinstance tests only, so site_taxonomy resolves every edge and it introduces no unresolved
# call. Producers call it with a statically-constructed literal path.
# =============================================================================================


def _ast_self_naming_constants(source_path):
    """AST authority: the module-level SELF-NAMING string constants (NAME = "NAME") in a source file.

    Reads the source TEXT at `source_path` and parses it. A member is included iff, at MODULE level,
    there is an assignment whose right-hand side is a single string literal EQUAL to the assigned
    name (`FOO = "FOO"`) — the per-member declaration a closed-vocabulary enum uses. This is a
    structurally distinct second encoding of the vocabulary than the tuple/frozenset that aggregates
    the constants by name: perturbing that aggregate does not touch these declarations, so the
    authority is independent of the collection it certifies. No dynamic dispatch: the walk is a flat
    isinstance filter over `tree.body`, single-parameter and statically resolvable. Returns a set.
    """
    import ast
    import pathlib
    source_text = pathlib.Path(source_path).read_text(encoding="utf-8")
    tree = ast.parse(source_text)
    names = set()
    for statement in tree.body:
        if not isinstance(statement, ast.Assign):
            continue
        value = statement.value
        if not isinstance(value, ast.Constant):
            continue
        literal = value.value
        if not isinstance(literal, str):
            continue
        for target in statement.targets:
            if isinstance(target, ast.Name) and target.id == literal:
                names.add(target.id)
    return names


def startup_dispositions_authority(source_id):
    """P6 channel producer: derive the startup DISPOSITIONS vocabulary INDEPENDENTLY of the tuple.

    AST-extracts the self-naming constants (REQUIRED_AND_BOUND/ALLOWED_AND_BOUND/PROHIBITED/
    NOT_APPLICABLE) declared per-member in scripts/startup_policy.py — the sole isolable self-naming
    group in that module, which equals DISPOSITIONS exactly. Those per-member declarations are a
    different code object than the DISPOSITIONS tuple at :47, so this is a genuine second derivation
    of the vocabulary rather than a copy of the collection. `source_id` is the channel/source name
    the framework passes in; it is never the collection. Returns a set.
    """
    import pathlib
    source_path = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "startup_policy.py"
    return _ast_self_naming_constants(source_path)


def startup_dispositions_witness(payload):
    """Witness: OBSERVE the framework-injected startup-DISPOSITIONS authority and return the domain.

    Reads ONLY the framework-injected P6 channel content under `payload["_witness_inputs"]` — never
    a module global and never the collection. Unions every injected channel's members. Returns a set.
    """
    observed = set()
    for members in payload["_witness_inputs"].values():
        observed.update(members)
    return observed


def cache_classifications_authority(source_id):
    """P6 channel producer: derive the cache-authority CLASSIFICATIONS vocabulary INDEPENDENTLY.

    AST-extracts the self-naming constants (NON_AUTHORITATIVE_PERFORMANCE_HINT/…/
    PROHIBITED_FOR_TRUST_DECISIONS) declared per-member in scripts/cache_authority.py — the sole
    isolable self-naming group in that module, which equals CLASSIFICATIONS exactly. Those per-member
    declarations are a different code object than the CLASSIFICATIONS frozenset, so this is a genuine
    second derivation rather than a copy of the collection. `source_id` is the channel/source name;
    it is never the collection. Returns a set.
    """
    import pathlib
    source_path = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "cache_authority.py"
    return _ast_self_naming_constants(source_path)


def cache_classifications_witness(payload):
    """Witness: OBSERVE the framework-injected cache-CLASSIFICATIONS authority and return the domain.

    Reads ONLY the framework-injected P6 channel content under `payload["_witness_inputs"]` — never
    a module global and never the collection. Unions every injected channel's members. Returns a set.
    """
    observed = set()
    for members in payload["_witness_inputs"].values():
        observed.update(members)
    return observed


def external_trust_classifications_authority(source_id):
    """P6 channel producer: derive external_executable_trust CLASSIFICATIONS vocabulary INDEPENDENTLY.

    AST-extracts the self-naming constants (EXACT_PATH_AND_CONTENT_BOUND/…/TOOLCHAIN_IDENTITY_
    DELEGATED/PROHIBITED/NOT_APPLICABLE) declared per-member in scripts/external_executable_trust.py —
    the sole isolable self-naming group in that module, which equals CLASSIFICATIONS exactly. Those
    per-member declarations are a different code object than the CLASSIFICATIONS tuple, so this is a
    genuine second derivation rather than a copy of the collection. `source_id` is the channel/source
    name; it is never the collection. Returns a set.
    """
    import pathlib
    source_path = (pathlib.Path(__file__).resolve().parents[1]
                   / "scripts" / "external_executable_trust.py")
    return _ast_self_naming_constants(source_path)


def external_trust_classifications_witness(payload):
    """Witness: OBSERVE the framework-injected external-trust CLASSIFICATIONS authority; return domain.

    Reads ONLY the framework-injected P6 channel content under `payload["_witness_inputs"]` — never
    a module global and never the collection. Unions every injected channel's members. Returns a set.
    """
    observed = set()
    for members in payload["_witness_inputs"].values():
        observed.update(members)
    return observed


def leak_decisions_authority(source_id):
    """P6 channel producer: derive the leak_scan DECISIONS vocabulary INDEPENDENTLY of the tuple.

    AST-extracts the self-naming constants (SCANNED/SKIPPED_BINARY/…/ERROR_OR_UNKNOWN) declared
    per-member in scripts/leak_scan.py — the sole isolable self-naming group in that module, which
    equals DECISIONS exactly. Those per-member declarations are a different code object than the
    DECISIONS tuple, so this is a genuine second derivation rather than a copy of the collection.
    `source_id` is the channel/source name; it is never the collection. Returns a set.
    """
    import pathlib
    source_path = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "leak_scan.py"
    return _ast_self_naming_constants(source_path)


def leak_decisions_witness(payload):
    """Witness: OBSERVE the framework-injected leak_scan DECISIONS authority and return the domain.

    Reads ONLY the framework-injected P6 channel content under `payload["_witness_inputs"]` — never
    a module global and never the collection. Unions every injected channel's members. Returns a set.
    """
    observed = set()
    for members in payload["_witness_inputs"].values():
        observed.update(members)
    return observed


# =============================================================================================
# WAVE 6B CONSUMERS (Gate 4N-I28BH-B-SLICE3 shard-b) — three more certificate-backed collections
# from TWO further sub-families, each authority a DIFFERENT code object than the collection:
#   * F5-QUALIFIED (external authored contract, direction-checked): leak_scan.py::ALLOWED_ACCOUNTS
#     ← the independently-authored approved-account registry fixture that leak_scan reads AS its
#     authority. leak_scan reconciles BOTH directions; the load-bearing "missing" direction
#     (a registered account the scanner's literal omits — `unused = registry - ALLOWED_ACCOUNTS`)
#     is REQUIRED_SUPERSET. The registry is the FLOOR, not a ceiling the literal is validated
#     within, and `approved_accounts()` is documented to deliberately NOT read ALLOWED_ACCOUNTS.
#   * CROSS-MODULE (E/F1): gen_role_bootstrap_policy.py::ALLOWED_TAG_KEYS ← the reviewed trust
#     manifest's tags_expectation keys (authored in a DIFFERENT module, trust_policies), and
#     trust_validator.py::ALLOWED_SERVICE_PRINCIPALS ← the service_principal declared by every
#     SERVICE_ROLE entry of the same module's ROLE_PURPOSE design authority (a different code
#     object than the allowlist, the landed _ASSURANCE_ROLES←_ASSURANCE_ROLE_BY_MODE precedent).
# Same shape/constraints: class-free, single-parameter, statically resolvable; each witness pinned.
# =============================================================================================


def allowed_accounts_authority(source_id):
    """P6 channel producer: derive the approved-account domain INDEPENDENTLY of ALLOWED_ACCOUNTS.

    Reads the independently authored approved-account registry (via leak_scan.approved_accounts(),
    the same tracked fixture leak_scan treats as its authority over which accounts are permitted)
    and returns the registered account ids. That registry is a different artifact than the
    ALLOWED_ACCOUNTS frozenset — approved_accounts() is documented to deliberately NOT read the
    literal — so this is a genuine second derivation of the permitted-account floor rather than a
    copy of the collection. `source_id` is the channel/source name; it is never the collection.
    Returns a set.
    """
    import leak_scan
    accounts = set()
    for account_id in leak_scan.approved_accounts():
        accounts.add(account_id)
    return accounts


def allowed_accounts_witness(payload):
    """Witness: OBSERVE the framework-injected approved-account authority and return the domain.

    Reads ONLY the framework-injected P6 channel content under `payload["_witness_inputs"]` — never
    a module global and never the collection. Unions every injected channel's members. Returns a set.
    """
    observed = set()
    for members in payload["_witness_inputs"].values():
        observed.update(members)
    return observed


def reviewed_tag_keys_authority(source_id):
    """P6 channel producer: derive the reviewed tag-key domain INDEPENDENTLY of ALLOWED_TAG_KEYS.

    Reads the reviewed trust manifest's tags_expectation keys (via
    gen_role_bootstrap_policy.reviewed_tag_key_domain(), which unions `tags_expectation` over
    trust_policies.trust_manifest() — authored in a DIFFERENT module from the trust documents). That
    manifest is a different code object than the ALLOWED_TAG_KEYS list and is documented to be
    deliberately NOT derived from it, so this is a genuine second derivation of the tag-key floor
    rather than a copy of the collection. `source_id` is the channel/source name; it is never the
    collection. Returns a set.
    """
    import gen_role_bootstrap_policy
    keys = set()
    for key in gen_role_bootstrap_policy.reviewed_tag_key_domain():
        keys.add(key)
    return keys


def reviewed_tag_keys_witness(payload):
    """Witness: OBSERVE the framework-injected reviewed tag-key authority and return the domain.

    Reads ONLY the framework-injected P6 channel content under `payload["_witness_inputs"]` — never
    a module global and never the collection. Unions every injected channel's members. Returns a set.
    """
    observed = set()
    for members in payload["_witness_inputs"].values():
        observed.update(members)
    return observed


def service_principals_authority(source_id):
    """P6 channel producer: derive the service-principal domain INDEPENDENTLY of the allowlist.

    Reads trust_validator.ROLE_PURPOSE — the per-role design-intent authority — and returns the
    `service_principal` declared by every SERVICE_ROLE entry. ROLE_PURPOSE is a different code object
    than the ALLOWED_SERVICE_PRINCIPALS set, so this is a genuine second derivation of the principals
    those roles legitimately need (the landed _ASSURANCE_ROLES←_ASSURANCE_ROLE_BY_MODE intra-module
    precedent) rather than a copy of the collection. `source_id` is the channel/source name; it is
    never the collection. Returns a set.
    """
    import trust_validator
    principals = set()
    for purpose in trust_validator.ROLE_PURPOSE.values():
        if purpose["kind"] == trust_validator.SERVICE_ROLE:
            principals.add(purpose["service_principal"])
    return principals


def service_principals_witness(payload):
    """Witness: OBSERVE the framework-injected service-principal authority and return the domain.

    Reads ONLY the framework-injected P6 channel content under `payload["_witness_inputs"]` — never
    a module global and never the collection. Unions every injected channel's members. Returns a set.
    """
    observed = set()
    for members in payload["_witness_inputs"].values():
        observed.update(members)
    return observed


# =============================================================================================
# WAVE 7 CONSUMERS (Gate 4N-I28BH-B-SLICE3 shard-b, ceiling sweep) — four more certificate-backed
# collections, each authority a DIFFERENT code object / artifact than the collection:
#   * F3 NAME-PREFIX variant: docker_boundary.py::SITE_DECISIONS ← the VALUES of the module's
#     ^SITE_ string-valued constants (SITE_PASS/SITE_FAIL/SITE_UNRESOLVED/SITE_UNSUPPORTED). The
#     tuple aggregates them by NAME; SITE_DECISIONS/SITE_CLASSIFICATIONS also carry the SITE_ name
#     but hold TUPLE values, so the string-value filter isolates exactly the 4 decision constants.
#   * CROSS-MODULE (E): workflow_graph_validator.py::_ASSURANCE_ROLE_BY_MODE (its KEYS) ← the VALUES
#     of workflow_assurance's ^MODE_ lifecycle-mode constants (a DIFFERENT module that OWNS and
#     dispatches the modes). A mode workflow_assurance defines that the validator's dict omits would
#     make a real assurance step go unrecognised.
#   * F5-QUALIFIED (external authored contract): docker_boundary.py::DOCKER_STEERING_CATEGORIES
#     (its KEYS) ← the prose category names the tracked docker-boundary-policy.json call-site records
#     REFERENCE (permitted/prohibited_steering entries that are NOT concrete steering-flag keys); and
#     gen_role_bootstrap_policy.py::READ_BACK_ACTIONS ← operator-closure-contract.json
#     role_bootstrap_closure.iam_read_after_create (the authored read-after-create closure; the
#     generator does NOT read the contract, so it is a genuine second derivation).
# Same shape/constraints: class-free, single-parameter producers/witnesses, statically resolvable;
# each witness byte-pinned in completeness_framework.WITNESS_PROVIDER_MANIFEST.
#
# NEW INFRASTRUCTURE — the AST name-prefix helper `_ast_prefix_string_constant_values`. Like
# `_ast_self_naming_constants` it is a plain module-level def with NO dynamic dispatch (flat
# isinstance filter over tree.body); it takes two parameters (a source path and a literal name
# prefix) and is always called with LITERAL args, so site_taxonomy resolves every edge and it
# introduces no unresolved call.
# =============================================================================================


def _ast_prefix_string_constant_values(source_path, name_prefix):
    """AST authority: the VALUES of module-level string constants whose NAME starts with a prefix.

    Reads the source TEXT at `source_path` and returns the string literal value of every module-level
    assignment `PREFIX... = "literal"`. Members carrying the prefix in their NAME but a non-string
    (e.g. tuple) value are excluded — that is what isolates the per-member decision/mode constants
    from an aggregate tuple that shares the prefix. This is a structurally distinct second encoding
    than any tuple/dict that references those constants, so the authority is independent of the
    collection it certifies. No dynamic dispatch; the walk is a flat isinstance filter over
    `tree.body`, and both arguments are passed as literals. Returns a set.
    """
    import ast
    import pathlib
    source_text = pathlib.Path(source_path).read_text(encoding="utf-8")
    tree = ast.parse(source_text)
    values = set()
    for statement in tree.body:
        if not isinstance(statement, ast.Assign):
            continue
        value = statement.value
        if not isinstance(value, ast.Constant):
            continue
        literal = value.value
        if not isinstance(literal, str):
            continue
        for target in statement.targets:
            if isinstance(target, ast.Name) and target.id.startswith(name_prefix):
                values.add(literal)
    return values


def site_decisions_authority(source_id):
    """P6 channel producer: derive the docker-boundary SITE_DECISIONS vocabulary INDEPENDENTLY.

    AST-extracts the VALUES of the ^SITE_ string-valued constants in scripts/docker_boundary.py
    (SITE_PASS/SITE_FAIL/SITE_UNRESOLVED/SITE_UNSUPPORTED) — the sole isolable such group, equal to
    SITE_DECISIONS exactly. Those per-member declarations are a different code object than the
    SITE_DECISIONS tuple, so this is a genuine second derivation rather than a copy of the collection.
    `source_id` is the channel/source name; it is never the collection. Returns a set.
    """
    import pathlib
    source_path = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "docker_boundary.py"
    return _ast_prefix_string_constant_values(source_path, "SITE_")


def site_decisions_witness(payload):
    """Witness: OBSERVE the framework-injected SITE_DECISIONS authority and return the domain.

    Reads ONLY the framework-injected P6 channel content under `payload["_witness_inputs"]` — never
    a module global and never the collection. Unions every injected channel's members. Returns a set.
    """
    observed = set()
    for members in payload["_witness_inputs"].values():
        observed.update(members)
    return observed


def assurance_modes_authority(source_id):
    """P6 channel producer: derive the assurance MODE domain INDEPENDENTLY of _ASSURANCE_ROLE_BY_MODE.

    AST-extracts the VALUES of workflow_assurance's ^MODE_ constants (establish/pre_build_verify/
    post_build_image_bind/pre_push_verify) — a DIFFERENT module that OWNS and dispatches the lifecycle
    modes. Those are a different code object than the _ASSURANCE_ROLE_BY_MODE dict whose KEYS the
    validator recognises, so this is a genuine second derivation of the mode universe. `source_id` is
    the channel/source name; it is never the collection. Returns a set.
    """
    import pathlib
    source_path = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "workflow_assurance.py"
    return _ast_prefix_string_constant_values(source_path, "MODE_")


def assurance_modes_witness(payload):
    """Witness: OBSERVE the framework-injected assurance-MODE authority and return the domain.

    Reads ONLY the framework-injected P6 channel content under `payload["_witness_inputs"]` — never
    a module global and never the collection. Unions every injected channel's members. Returns a set.
    """
    observed = set()
    for members in payload["_witness_inputs"].values():
        observed.update(members)
    return observed


def docker_steering_categories_authority(source_id):
    """P6 channel producer: derive the referenced docker-steering CATEGORY domain INDEPENDENTLY.

    Reads the tracked docker-boundary-policy.json (the fixture docker_boundary adjudicates against)
    and returns every prose category name the call-site records REFERENCE in permitted_steering /
    prohibited_steering that is NOT a concrete steering-flag key. adjudicate_site fails closed on a
    referenced category absent from DOCKER_STEERING_CATEGORIES, so that dict must COVER these — and
    the fixture is a different artifact than the dict, so this is a genuine second derivation.
    `source_id` is the channel/source name; it is never the collection. Returns a set.
    """
    import json
    import pathlib
    policy_path = (pathlib.Path(__file__).resolve().parents[1]
                   / "tests" / "fixtures" / "docker-boundary-policy.json")
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    steering_keys = set(policy.get("steering", {}))
    categories = set()
    for record in policy.get("call_sites", []):
        for entry in record.get("permitted_steering", []):
            if entry not in steering_keys:
                categories.add(entry)
        for entry in record.get("prohibited_steering", []):
            if entry not in steering_keys:
                categories.add(entry)
    return categories


def docker_steering_categories_witness(payload):
    """Witness: OBSERVE the framework-injected docker-steering-category authority; return the domain.

    Reads ONLY the framework-injected P6 channel content under `payload["_witness_inputs"]` — never
    a module global and never the collection. Unions every injected channel's members. Returns a set.
    """
    observed = set()
    for members in payload["_witness_inputs"].values():
        observed.update(members)
    return observed


def read_back_actions_authority(source_id):
    """P6 channel producer: derive the read-after-create action floor INDEPENDENTLY of READ_BACK_ACTIONS.

    Reads operator-closure-contract.json role_bootstrap_closure.iam_read_after_create — the authored
    closure of IAM reads the role-bootstrap operator performs after CreateRole. gen_role_bootstrap_policy
    does NOT read this contract, so it is a genuine second derivation of the read-back set rather than a
    copy of the READ_BACK_ACTIONS tuple. `source_id` is the channel/source name; it is never the
    collection. Returns a set.
    """
    import json
    import pathlib
    contract_path = (pathlib.Path(__file__).resolve().parents[1]
                     / "infra" / "aws" / "operator-closure-contract.json")
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    actions = set()
    for action in contract["role_bootstrap_closure"]["iam_read_after_create"]:
        actions.add(action)
    return actions


def read_back_actions_witness(payload):
    """Witness: OBSERVE the framework-injected read-after-create authority and return the domain.

    Reads ONLY the framework-injected P6 channel content under `payload["_witness_inputs"]` — never
    a module global and never the collection. Unions every injected channel's members. Returns a set.
    """
    observed = set()
    for members in payload["_witness_inputs"].values():
        observed.update(members)
    return observed
