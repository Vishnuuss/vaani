import type { Lead } from '@/types';

function escapeCsvValue(val: any): string {
  if (val === null || val === undefined) {
    return '';
  }
  const str = String(val);
  // If the string contains comma, quote, or newline, it must be enclosed in quotes
  if (str.includes(',') || str.includes('"') || str.includes('\n')) {
    // Escape double quotes by doubling them
    return `"${str.replace(/"/g, '""')}"`;
  }
  return str;
}

/**
 * The CSV handed to Dograh for a campaign.
 *
 * `vertical` is included so the business line reaches the call itself: Dograh
 * copies these columns into `initial_context`, the webhook template can echo
 * one back, and the result handler can then tell that a solar call belongs to a
 * solar lead. Without it every call came back with no business line at all, and
 * the cross-line safety check in the webhook had nothing to compare against.
 */
export function buildCampaignCsv(
  leads: Pick<Lead, 'id' | 'name' | 'phone' | 'city' | 'property_type' | 'budget' | 'email'>[],
  vertical?: string,
): string {
  const headers = ['phone_number', 'customer_name', 'city', 'property_type', 'budget', 'lead_id', 'email', 'vertical'];
  
  const rows = leads.map(lead => {
    return [
      escapeCsvValue(lead.phone),
      escapeCsvValue(lead.name),
      escapeCsvValue(lead.city),
      escapeCsvValue(lead.property_type),
      escapeCsvValue(lead.budget),
      escapeCsvValue(lead.id),
      escapeCsvValue(lead.email),
      escapeCsvValue(vertical ?? '')
    ].join(',');
  });

  return [headers.join(','), ...rows].join('\n');
}
