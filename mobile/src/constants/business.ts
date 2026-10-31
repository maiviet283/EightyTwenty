/**
 * Business rules and contact details taken from the approved prototype / website.
 * These should eventually come from the backend (see docs/BACKEND_HANDOFF.md → "Configuration").
 */
export const ORDER_RULES = {
  /** Card payments carry a 2.95% surcharge. */
  cardSurchargeRate: 0.0295,
  /** Delivery adds 10% to the order. */
  deliveryRate: 0.1,
  /** Shown when a café has no published wait time. */
  defaultWait: '15–20 min',
  /** Scheduled pickup window for "Later today" (7:00 am – 2:30 pm, 30-min slots). */
  scheduleFromHour: 7,
  scheduleToHour: 14.5,
  maxItemQty: 20,
  maxLineQtyInput: 99,
} as const;

export const LOYALTY_RULES = {
  /** Nine stamps — the 10th coffee is free. */
  stampsPerReward: 9,
} as const;

export const BOOKING_RULES = {
  /** "Bookings need at least 12 hours' notice." */
  minNoticeHours: 12,
  /** Last booking 30 minutes before close. */
  lastBookingBeforeCloseMinutes: 30,
  defaultGuests: 2,
} as const;

export const CONTACT = {
  infoEmail: 'info@eightytwentyfood.com.au',
  /** Same-day bookings by SMS. */
  sameDaySms: '0422 573 102',
  website: 'https://eightytwentyfood.com.au',
} as const;

export const GIFT_CARD_AMOUNTS = [25, 50, 75, 100] as const;

/**
 * Cafés kept out of every customer-facing list, matching the website (`publicLocations`).
 * Akuna St is listed in BOS with its history intact but is not open to the public yet.
 */
export const HIDDEN_LOCATION_IDS: readonly string[] = ['akuna'];

/** Simulated live-order stage interval in the prototype (ms). Used by the mock order service. */
export const MOCK_ORDER_STAGE_INTERVAL_MS = 4200;
