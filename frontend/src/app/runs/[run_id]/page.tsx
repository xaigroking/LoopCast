import { WorkspacePage } from '@/components/workspace-page';

export const dynamic = 'force-dynamic';

export default async function RunPage({ params }: { params: Promise<{ run_id: string }> }) {
  const { run_id } = await params;
  return <WorkspacePage kind="detail" runId={run_id} />;
}
