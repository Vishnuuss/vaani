import { handleCampaignAction } from '@/lib/campaign-actions';

export async function POST(_request: Request, { params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return handleCampaignAction('pause', id);
}
