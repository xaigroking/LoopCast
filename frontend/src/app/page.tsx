import { WorkspacePage } from '@/components/workspace-page';

export const dynamic = 'force-dynamic';

export default function HomePage() {
  return <WorkspacePage kind="list" />;
}
