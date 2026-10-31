import { validateCateringEnquiry } from '@/domain/catering';
import { isHiddenLocation, publicLocations } from '@/domain/locations';
import type { CateringRepository, ContentRepository, LocationRepository, MenuRepository } from '@/repositories/contracts';
import { AppError } from '@/services/errors';
import type { CateringEnquiry, CateringEnquiryRequest, CateringPackage, Faq, ID, Location, MenuCategory, MenuItem, Offer } from '@/types';
import { localId } from '@/utils/id';
import { latency } from '../db';
import { CATEGORIES, CATERING_PACKAGES, LOCATIONS, MENU, OFFERS, findLocation, findMenuItem } from '../catalog';
import { FAQ_RECORDS } from '../fixtures/content';
import { clone, MockRepository, nowIso } from './shared';

export class MockLocationRepository extends MockRepository implements LocationRepository {
  async getLocations(): Promise<Location[]> {
    await latency();
    return publicLocations(LOCATIONS);
  }

  async getLocation(id: ID): Promise<Location> {
    await latency();
    const loc = isHiddenLocation(id) ? undefined : findLocation(id);
    if (!loc) throw new AppError('NOT_FOUND', 'That café could not be found.', { status: 404 });
    return loc;
  }
}

export class MockMenuRepository extends MockRepository implements MenuRepository {
  async getCategories(): Promise<MenuCategory[]> {
    await latency();
    return CATEGORIES;
  }

  /** TODO(backend): the export has a single shared menu; per-café availability isn't in the source data. */
  async getMenuItems(locationId: ID): Promise<MenuItem[]> {
    await latency();
    if (!findLocation(locationId)) throw new AppError('NOT_FOUND', 'That café could not be found.', { status: 404 });
    return MENU;
  }

  async getItemById(id: ID): Promise<MenuItem> {
    await latency();
    const item = findMenuItem(id);
    if (!item) throw new AppError('NOT_FOUND', 'That item is no longer on the menu.', { status: 404 });
    return item;
  }
}

export class MockCateringRepository extends MockRepository implements CateringRepository {
  async getPackages(): Promise<CateringPackage[]> {
    await latency();
    return CATERING_PACKAGES;
  }

  async getEnquiries(): Promise<CateringEnquiry[]> {
    return clone((await this.me()).enquiries);
  }

  async createEnquiry(req: CateringEnquiryRequest): Promise<CateringEnquiry> {
    const me = await this.me();
    const pkg = CATERING_PACKAGES.find((p) => p.id === req.packageId);
    if (!pkg) throw new AppError('VALIDATION', 'Choose a catering menu.', { fieldErrors: { packageId: 'invalid' } });
    // Server-side re-validation with the same rules as the form.
    const invalid = Object.keys(validateCateringEnquiry(req));
    if (invalid.length) throw new AppError('VALIDATION', 'Please check the highlighted fields.', { fieldErrors: Object.fromEntries(invalid.map((f) => [f, 'invalid'])) });
    const enquiry: CateringEnquiry = {
      id: localId('cat_'),
      packageId: pkg.id,
      packageName: pkg.name,
      guests: req.guests,
      date: req.date,
      occasion: req.occasion.trim() || 'Catering',
      status: 'Enquiry sent',
      createdAt: nowIso(),
    };
    me.enquiries.unshift(enquiry);
    this.commit();
    return clone(enquiry);
  }
}

export class MockContentRepository extends MockRepository implements ContentRepository {
  async getOffers(): Promise<Offer[]> {
    await latency();
    return OFFERS;
  }

  async getFaqs(): Promise<Faq[]> {
    await latency();
    return FAQ_RECORDS;
  }
}
