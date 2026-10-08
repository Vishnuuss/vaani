export function validatePhoneNumber(phone: string): string | null {
  if (!phone) return null;
  
  // Remove all non-digit characters except plus
  let cleaned = phone.replace(/[^\d+]/g, '');
  
  // Handle Indian phone numbers starting with 0
  if (cleaned.startsWith('0')) {
    cleaned = cleaned.substring(1);
  }

  // Handle numbers that don't have country code but are 10 digits
  if (cleaned.length === 10) {
    return `+91${cleaned}`;
  }

  // Handle numbers with 91 prefix but no plus
  if (cleaned.length === 12 && cleaned.startsWith('91')) {
    return `+${cleaned}`;
  }
  
  // Handle properly formatted +91 numbers
  if (cleaned.length === 13 && cleaned.startsWith('+91')) {
    return cleaned;
  }

  // General E.164 validation as fallback
  if (cleaned.startsWith('+') && cleaned.length >= 10 && cleaned.length <= 15) {
    return cleaned;
  }

  return null;
}

export function sanitizeString(str: string, maxLength: number): string {
  if (!str || typeof str !== 'string') return '';
  // Remove potentially dangerous characters and trim
  const sanitized = str.replace(/[<>]/g, '').trim();
  return sanitized.substring(0, maxLength);
}

export function validateCsvRow(row: Record<string, string>): { valid: boolean; cleaned: any; reason?: string } {
  if (!row || typeof row !== 'object') {
    return { valid: false, cleaned: {}, reason: 'Invalid row object' };
  }

  const cleaned: Record<string, any> = {};
  
  // Validate phone
  const phoneVal = row['Phone'] || row['phone'] || row['Phone Number'] || row['phone_number'];
  if (!phoneVal) {
    return { valid: false, cleaned: {}, reason: 'Missing phone number' };
  }
  
  const validPhone = validatePhoneNumber(phoneVal);
  if (!validPhone) {
    return { valid: false, cleaned: {}, reason: 'Invalid phone number format' };
  }
  cleaned.phone = validPhone;

  // Name
  cleaned.name = row['Name'] || row['name'] || row['Customer Name'] || row['customer_name'] || null;
  if (cleaned.name) {
    cleaned.name = sanitizeString(cleaned.name, 100);
  }

  // Email
  cleaned.email = row['Email'] || row['email'] || null;
  if (cleaned.email && !/^\S+@\S+\.\S+$/.test(cleaned.email)) {
    cleaned.email = null; // invalid email, just null it out rather than rejecting row
  }

  // Other fields
  cleaned.city = sanitizeString(row['City'] || row['city'] || '', 50) || null;
  cleaned.property_type = sanitizeString(row['Property Type'] || row['property_type'] || '', 50) || null;
  cleaned.budget = sanitizeString(row['Budget'] || row['budget'] || '', 50) || null;
  cleaned.source = sanitizeString(row['Source'] || row['source'] || 'CSV Upload', 50);

  return { valid: true, cleaned };
}

export function validateCampaignParams(params: any): { valid: boolean; reason?: string } {
  if (!params) return { valid: false, reason: 'Missing params' };
  if (!params.name || typeof params.name !== 'string' || params.name.trim() === '') {
    return { valid: false, reason: 'Campaign name is required' };
  }
  if (!params.workflow_id || typeof params.workflow_id !== 'number') {
    return { valid: false, reason: 'Workflow ID is required and must be a number' };
  }
  if (!params.source_id || typeof params.source_id !== 'string') {
    return { valid: false, reason: 'Source ID is required' };
  }
  return { valid: true };
}
