import { apiRequest } from './api';
import type { components } from './api-schema';

export type Schedule = components['schemas']['ScheduleRecord'];
export type ScheduleSpec = Schedule['spec'];
export type ScheduleGroup = components['schemas']['ReleaseGroup'];
export type SchedulePreview = components['schemas']['SchedulePreview'];
export type ScheduleCapability = components['schemas']['ScheduleCapability'];
export type ScheduleList = components['schemas']['ScheduleList'];
export type OccurrenceList = components['schemas']['ScheduleOccurrenceList'];

export async function previewSchedule(spec: ScheduleSpec, plan?: Pick<Schedule, 'id' | 'control_version'>): Promise<SchedulePreview> {
  const body = { spec, schedule_id: plan?.id ?? null, expected_version: plan?.control_version ?? null };
  let result: SchedulePreview | null = null;
  let cursor: string | null = null;
  // A finite rule has at most 366 dates, with at most twelve preview pages.
  for (let page = 0; page < 12; page++) {
    const next: SchedulePreview = await apiRequest('/v1/studio/schedules/preview', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ...body, cursor }),
    });
    result = result ? { ...next, items: [...result.items, ...next.items] } : next;
    cursor = next.next_cursor;
    if (!cursor) return result;
  }
  throw Error('PREVIEW_EXPIRED');
}
