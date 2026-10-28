import { describe, expect, it, vi } from 'vitest';
import type { OrganizationExportOut, OrganizationExportPageOut } from '@/api/types';
import { assembleOrganizationExport, type ExportSource } from '@/lib/organization-export';

// D-9f: the assembly walks next_cursor to null per table and never returns a partial file.
const SUBJECT = { type: 'organization', user_id: null, organization_id: 'o' } as const;
const index = (next: string | null): OrganizationExportOut => ({
  schema_version: 2,
  generated_at: 't',
  subject: SUBJECT,
  page_size_default: 2,
  page_size_cap: 2,
  complete: next === null,
  consistency: 'c',
  members: [],
  tables: { t: { row_count: 4, rows: [{ id: 1 }, { id: 2 }], next_cursor: next } },
  not_exported: [],
});
const page = (rows: number[], next: string | null): OrganizationExportPageOut => ({
  schema_version: 2,
  generated_at: 't',
  subject: SUBJECT,
  table: 't',
  limit: 2,
  rows: rows.map((id) => ({ id })),
  next_cursor: next,
});

describe('assembleOrganizationExport', () => {
  it('follows cursors to the end and reports progress', async () => {
    const source: ExportSource = {
      index: async () => index('a'),
      page: vi.fn(async (_t, cursor) => (cursor === 'a' ? page([3], 'b') : page([4], null))),
    };
    const progress = vi.fn();
    const out = await assembleOrganizationExport(source, { onProgress: progress });
    expect(out.tables.t!.rows.map((r) => r.id)).toEqual([1, 2, 3, 4]);
    expect(out.assembly).toMatchObject({ pages_fetched: 3, changed_during_download: false });
    expect(progress).toHaveBeenLastCalledWith({ tablesDone: 1, tablesTotal: 1, rowsReceived: 4, rowsExpected: 4 });
  });

  it('rejects when a page fails or the server repeats a cursor (no partial file)', async () => {
    const failing: ExportSource = {
      index: async () => index('a'),
      page: async () => {
        throw new Error('500');
      },
    };
    await expect(assembleOrganizationExport(failing)).rejects.toThrow('500');
    const looping: ExportSource = { index: async () => index('a'), page: async () => page([9], 'a') };
    await expect(assembleOrganizationExport(looping)).rejects.toThrow(/did not advance/);
  });

  it('stops when aborted', async () => {
    const ctl = new AbortController();
    const source: ExportSource = {
      index: async () => index('a'),
      page: async () => {
        ctl.abort();
        return page([3], 'b');
      },
    };
    await expect(assembleOrganizationExport(source, { signal: ctl.signal })).rejects.toThrow();
  });
});
