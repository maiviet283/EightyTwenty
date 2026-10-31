import { HIDDEN_LOCATION_IDS } from '@/constants/business';
import type { Location } from '@/types';

/** True for a café the app never shows customers (see `HIDDEN_LOCATION_IDS`). */
export function isHiddenLocation(id: string): boolean {
  return HIDDEN_LOCATION_IDS.includes(id);
}

/**
 * The cafés customers may see, pick, order from or book: the server's list without the
 * hidden ones. Applied where locations enter the app (both repositories), so screens,
 * pickers, the home rail and cached query data all agree with the website.
 */
export function publicLocations<T extends Pick<Location, 'id'>>(locations: T[]): T[] {
  return locations.filter((l) => !isHiddenLocation(l.id));
}
