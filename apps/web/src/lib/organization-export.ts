import type { OrganizationExportOut, OrganizationExportPageOut } from '@/api/types';

/**
 * Assemble the complete organization export from its index and keyset pages (D-9f).
 *
 * `GET /organizations/{id}/export` returns each table's exact `row_count`, its first page
 * and a `next_cursor`; `GET /organizations/{id}/export/{table}?cursor=&limit=` returns the
 * following pages until `next_cursor` is null. This walks every table to its end, in the
 * index's table order, then returns ONE document. Any failed request (non-2xx, network
 * error, abort) rejects: no partial file is ever produced.
 *
 * File layout (`schema_version` 2) -- the index, with each table completed:
 *   { schema_version, generated_at, subject, members, not_exported, consistency,
 *     page_size_default, page_size_cap, complete,
 *     assembly: { started_at, finished_at, pages_fetched, changed_during_download },
 *     tables: { <name>: { row_count, rows_received, rows: [...every row...] } } }
 * `row_count` is the table's exact count when the index was read; `rows_received` is what
 * the pages delivered. They differ only if rows were created or deleted during the download
 * (`changed_during_download` is then true; false means only that the counts matched, not that
 * nothing changed -- a created and a deleted row cancel out): there is no snapshot isolation, a row created
 * after the walk passed its position is not included, a row deleted before its page was
 * read is absent.
 */
export interface ExportProgress {
  tablesDone: number;
  tablesTotal: number;
  rowsReceived: number;
  rowsExpected: number;
}

export interface AssembledTable {
  row_count: number;
  rows_received: number;
  rows: Record<string, unknown>[];
}

export interface AssembledOrganizationExport
  extends Omit<OrganizationExportOut, 'tables' | 'complete'> {
  complete: true;
  assembly: {
    started_at: string;
    finished_at: string;
    pages_fetched: number;
    changed_during_download: boolean;
  };
  tables: Record<string, AssembledTable>;
}

export interface ExportSource {
  index: (signal?: AbortSignal) => Promise<OrganizationExportOut>;
  page: (
    table: string,
    cursor: string,
    limit: number,
    signal?: AbortSignal,
  ) => Promise<OrganizationExportPageOut>;
}

export async function assembleOrganizationExport(
  source: ExportSource,
  options: { signal?: AbortSignal; onProgress?: (p: ExportProgress) => void } = {},
): Promise<AssembledOrganizationExport> {
  const { signal, onProgress } = options;
  const startedAt = new Date().toISOString();
  const index = await source.index(signal);
  const names = Object.keys(index.tables);
  const expected = names.reduce((sum, n) => sum + index.tables[n]!.row_count, 0);
  let received = 0;
  let pages = 1;
  const tables: Record<string, AssembledTable> = {};
  const report = (done: number) =>
    onProgress?.({ tablesDone: done, tablesTotal: names.length, rowsReceived: received, rowsExpected: expected });
  report(0);
  for (const [i, name] of names.entries()) {
    const entry = index.tables[name]!;
    const rows: Record<string, unknown>[] = [...entry.rows];
    received += entry.rows.length;
    let cursor = entry.next_cursor;
    const seen = new Set<string>();
    while (cursor !== null && cursor !== undefined) {
      if (seen.has(cursor)) throw new Error(`The export of ${name} did not advance.`);
      seen.add(cursor);
      if (signal?.aborted) throw new DOMException('The download was cancelled.', 'AbortError');
      // The server bounds a page at page_size_cap; ask for the most it allows.
      const page = await source.page(name, cursor, index.page_size_cap, signal);
      pages += 1;
      rows.push(...page.rows);
      received += page.rows.length;
      cursor = page.next_cursor;
      report(i);
    }
    tables[name] = { row_count: entry.row_count, rows_received: rows.length, rows };
    report(i + 1);
  }
  const changed = names.some((n) => tables[n]!.rows_received !== tables[n]!.row_count);
  return {
    ...index,
    complete: true,
    assembly: {
      started_at: startedAt,
      finished_at: new Date().toISOString(),
      pages_fetched: pages,
      changed_during_download: changed,
    },
    tables,
  };
}
