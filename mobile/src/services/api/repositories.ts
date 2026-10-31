import type {
  AuthRepository,
  CateringRepository,
  ContentRepository,
  CustomerRepository,
  GiftCardRepository,
  LocationRepository,
  LoyaltyRepository,
  MenuRepository,
  MutationOptions,
  NotificationFeed,
  NotificationRepository,
  OrderRepository,
  PaymentRepository,
  PreferencesRepository,
  ReservationRepository,
  SignInRequest,
  SignUpRequest,
} from '@/repositories/contracts';
import { isHiddenLocation, publicLocations } from '@/domain/locations';
import { AppError, isTransientError } from '@/services/errors';
import type {
  CardTokenResult,
  CateringEnquiry,
  CateringEnquiryRequest,
  CateringPackage,
  CreateOrderRequest,
  Customer,
  CustomerPreferences,
  CustomerUpdate,
  Faq,
  GiftCard,
  GiftCardActivity,
  ID,
  Location,
  LoyaltyActivity,
  LoyaltyCard,
  MenuCategory,
  MenuItem,
  Offer,
  Order,
  Page,
  PageRequest,
  Reservation,
  ReservationRequest,
  SavedAddress,
  SavedCard,
  SendGiftCardRequest,
  Session,
} from '@/types';
import { localId } from '@/utils/id';
import type { HttpClient } from './client';
import type * as D from './dto';
import { endpoints } from './endpoints';
import * as M from './mappers';

/**
 * HTTP repositories (EXPO_PUBLIC_DATA_SOURCE=api).
 * PENDING BACKEND: every endpoint here is a proposal documented in docs/API_CONTRACTS.md.
 */

export class HttpAuthRepository implements AuthRepository {
  constructor(private readonly http: HttpClient) {}

  async signIn(req: SignInRequest): Promise<Session> {
    return M.sessionFromDto(await this.http.post<D.SessionDTO>(endpoints.auth.signIn, req, { auth: false }));
  }
  async signUp(req: SignUpRequest): Promise<Session> {
    return M.sessionFromDto(await this.http.post<D.SessionDTO>(endpoints.auth.signUp, req, { auth: false }));
  }
  async signOut(session: Session): Promise<void> {
    await this.http.post(endpoints.auth.signOut, { refreshToken: session.refreshToken });
  }
  async refresh(refreshToken: string): Promise<Session> {
    try {
      return M.sessionFromDto(await this.http.post<D.SessionDTO>(endpoints.auth.refresh, { refreshToken }, { auth: false }));
    } catch (e) {
      // Network trouble is not an expired session — let the caller keep the tokens.
      if (isTransientError(e)) throw e;
      throw new AppError('SESSION_EXPIRED', 'Your session has expired. Please log in again.', { status: 401, cause: e });
    }
  }
  async requestPasswordReset(email: string): Promise<void> {
    await this.http.post(endpoints.auth.passwordReset, { email }, { auth: false });
  }
  async changePassword(currentPassword: string, newPassword: string): Promise<Session> {
    return M.sessionFromDto(await this.http.post<D.SessionDTO>(endpoints.me.password, { currentPassword, newPassword }));
  }
}

export class HttpCustomerRepository implements CustomerRepository {
  constructor(private readonly http: HttpClient) {}
  async getProfile(): Promise<Customer> {
    return M.customerFromDto(await this.http.get<D.CustomerDTO>(endpoints.me.profile));
  }
  async updateProfile(update: CustomerUpdate): Promise<Customer> {
    return M.customerFromDto(await this.http.patch<D.CustomerDTO>(endpoints.me.profile, update));
  }
  async requestAccountClosure(): Promise<void> {
    await this.http.post(endpoints.me.closureRequest);
  }
  async deleteAccount(): Promise<void> {
    await this.http.delete(endpoints.me.profile);
  }
}

export class HttpPreferencesRepository implements PreferencesRepository {
  constructor(private readonly http: HttpClient) {}
  async getPreferences(): Promise<CustomerPreferences> {
    return M.preferencesFromDto(await this.http.get<D.PreferencesDTO>(endpoints.me.preferences));
  }
  async updatePreferences(patch: Partial<CustomerPreferences>): Promise<CustomerPreferences> {
    return M.preferencesFromDto(await this.http.patch<D.PreferencesDTO>(endpoints.me.preferences, M.preferencesToDto(patch)));
  }
  async getFavourites(): Promise<ID[]> {
    return (await this.http.get<{ itemIds: string[] }>(endpoints.me.favourites)).itemIds;
  }
  async setFavourite(itemId: ID, favourite: boolean): Promise<ID[]> {
    const path = endpoints.me.favourite(itemId);
    const res = favourite ? await this.http.put<{ itemIds: string[] }>(path) : await this.http.delete<{ itemIds: string[] }>(path);
    return res.itemIds;
  }
  async getAddresses(): Promise<SavedAddress[]> {
    return (await this.http.get<D.AddressDTO[]>(endpoints.me.addresses)).map(M.addressFromDto);
  }
  async addAddress(address: Omit<SavedAddress, 'id'>): Promise<SavedAddress> {
    const body = { label: address.label.toLowerCase(), line: address.line };
    return M.addressFromDto(await this.http.post<D.AddressDTO>(endpoints.me.addresses, body));
  }
  async removeAddress(id: ID): Promise<void> {
    await this.http.delete(endpoints.me.address(id));
  }
}

export class HttpLocationRepository implements LocationRepository {
  constructor(private readonly http: HttpClient) {}
  async getLocations(): Promise<Location[]> {
    return publicLocations((await this.http.get<D.LocationDTO[]>(endpoints.locations, { auth: false })).map(M.locationFromDto));
  }
  async getLocation(id: ID): Promise<Location> {
    if (isHiddenLocation(id)) throw new AppError('NOT_FOUND', 'That café could not be found.', { status: 404 });
    return M.locationFromDto(await this.http.get<D.LocationDTO>(endpoints.location(id), { auth: false }));
  }
}

export class HttpMenuRepository implements MenuRepository {
  constructor(private readonly http: HttpClient) {}
  async getCategories(): Promise<MenuCategory[]> {
    return (await this.http.get<D.MenuCategoryDTO[]>(endpoints.menuCategories, { auth: false })).map(M.categoryFromDto);
  }
  async getMenuItems(locationId: ID): Promise<MenuItem[]> {
    return (await this.http.get<D.MenuItemDTO[]>(endpoints.locationMenu(locationId), { auth: false })).map(M.menuItemFromDto);
  }
  async getItemById(id: ID): Promise<MenuItem> {
    return M.menuItemFromDto(await this.http.get<D.MenuItemDTO>(endpoints.menuItem(id), { auth: false }));
  }
}

export class HttpOrderRepository implements OrderRepository {
  constructor(private readonly http: HttpClient) {}
  async createOrder(req: CreateOrderRequest, options: MutationOptions = {}): Promise<Order> {
    const body = {
      locationId: req.locationId,
      method: M.methodToDto[req.method],
      timing: req.timing,
      deliveryAddress: req.deliveryAddress ?? null,
      tableNumber: req.tableNumber ?? null,
      lines: req.lines,
      payment: req.payment,
      useGiftCard: req.useGiftCard,
      useReward: req.useReward,
      contact: req.contact,
      expectedTotalCents: M.toCents(req.expectedTotal),
    };
    // The key is reused when the customer retries after a timeout, so the order is charged once.
    return M.orderFromDto(await this.http.post<D.OrderDTO>(endpoints.orders, body, { idempotencyKey: options.idempotencyKey ?? localId('ord_') }));
  }
  async getOrder(id: ID): Promise<Order> {
    return M.orderFromDto(await this.http.get<D.OrderDTO>(endpoints.order(id)));
  }
  async getOrderHistory(page: PageRequest = {}): Promise<Page<Order>> {
    const res = await this.http.get<D.PageDTO<D.OrderDTO>>(endpoints.me.orders, { query: { cursor: page.cursor, limit: page.limit } });
    return { items: res.items.map(M.orderFromDto), nextCursor: res.nextCursor };
  }
}

export class HttpPaymentRepository implements PaymentRepository {
  constructor(private readonly http: HttpClient) {}
  async getPaymentMethods(): Promise<SavedCard[]> {
    return (await this.http.get<D.SavedCardDTO[]>(endpoints.me.paymentMethods)).map(M.savedCardFromDto);
  }
  /** Sends only the provider's single-use token (e.g. Square card nonce). */
  async addCard(card: CardTokenResult): Promise<SavedCard> {
    return M.savedCardFromDto(await this.http.post<D.SavedCardDTO>(endpoints.me.paymentMethods, { sourceToken: card.token }));
  }
  async removePaymentMethod(id: ID): Promise<void> {
    await this.http.delete(endpoints.me.paymentMethod(id));
  }
}

export class HttpLoyaltyRepository implements LoyaltyRepository {
  constructor(private readonly http: HttpClient) {}
  async getCard(): Promise<LoyaltyCard> {
    return M.loyaltyCardFromDto(await this.http.get<D.LoyaltyCardDTO>(endpoints.me.loyalty));
  }
  async getActivity(page: PageRequest = {}): Promise<Page<LoyaltyActivity>> {
    const res = await this.http.get<D.PageDTO<D.LoyaltyActivityDTO>>(endpoints.me.loyaltyActivity, { query: { cursor: page.cursor, limit: page.limit } });
    return { items: res.items.map(M.loyaltyActivityFromDto), nextCursor: res.nextCursor };
  }
  async getWalletPassUrl(returnUrl?: string): Promise<string | null> {
    try {
      return (await this.http.get<{ url: string }>(endpoints.me.walletPass, { query: { return: returnUrl } })).url;
    } catch (e) {
      if (e instanceof AppError && e.code === 'NOT_FOUND') return null;
      throw e;
    }
  }
  async markWalletPassAdded(): Promise<LoyaltyCard> {
    return M.loyaltyCardFromDto(await this.http.post<D.LoyaltyCardDTO>(endpoints.me.walletPassAdded));
  }
}

export class HttpReservationRepository implements ReservationRepository {
  constructor(private readonly http: HttpClient) {}
  async getReservations(): Promise<Reservation[]> {
    return (await this.http.get<D.ReservationDTO[]>(endpoints.me.reservations)).map(M.reservationFromDto);
  }
  async createReservation(req: ReservationRequest): Promise<Reservation> {
    return M.reservationFromDto(await this.http.post<D.ReservationDTO>(endpoints.reservations, { ...req, seating: req.seating.toLowerCase() }));
  }
  async cancelReservation(id: ID): Promise<void> {
    await this.http.delete(endpoints.reservation(id));
  }
}

export class HttpCateringRepository implements CateringRepository {
  constructor(private readonly http: HttpClient) {}
  async getPackages(): Promise<CateringPackage[]> {
    return (await this.http.get<D.CateringPackageDTO[]>(endpoints.cateringPackages, { auth: false })).map(M.cateringPackageFromDto);
  }
  async getEnquiries(): Promise<CateringEnquiry[]> {
    return (await this.http.get<D.CateringEnquiryDTO[]>(endpoints.me.cateringEnquiries)).map(M.cateringEnquiryFromDto);
  }
  async createEnquiry(req: CateringEnquiryRequest): Promise<CateringEnquiry> {
    return M.cateringEnquiryFromDto(await this.http.post<D.CateringEnquiryDTO>(endpoints.cateringEnquiries, req));
  }
}

export class HttpGiftCardRepository implements GiftCardRepository {
  constructor(private readonly http: HttpClient) {}
  async getGiftCard(): Promise<GiftCard> {
    return M.giftCardFromDto(await this.http.get<D.GiftCardDTO>(endpoints.me.giftCard));
  }
  async getActivity(): Promise<GiftCardActivity[]> {
    return (await this.http.get<D.GiftCardActivityDTO[]>(endpoints.me.giftCardActivity)).map(M.giftActivityFromDto);
  }
  async sendGiftCard(req: SendGiftCardRequest, options: MutationOptions = {}): Promise<GiftCardActivity> {
    const body = { amountCents: M.toCents(req.amount), recipientName: req.recipientName, recipientEmail: req.recipientEmail, message: req.message, paymentToken: req.paymentToken };
    return M.giftActivityFromDto(await this.http.post<D.GiftCardActivityDTO>(endpoints.giftCardsSend, body, { idempotencyKey: options.idempotencyKey ?? localId('gift_') }));
  }
}

export class HttpNotificationRepository implements NotificationRepository {
  constructor(private readonly http: HttpClient) {}
  async getFeed(): Promise<NotificationFeed> {
    const res = await this.http.get<D.NotificationFeedDTO>(endpoints.me.notifications);
    const items = res.items.map(M.notificationFromDto);
    return { items, unseen: items.filter((n) => res.unseenIds.includes(n.id)) };
  }
  async markSeen(): Promise<void> {
    await this.http.post(endpoints.me.notificationsSeen);
  }
  async markAllRead(): Promise<void> {
    await this.http.post(endpoints.me.notificationsReadAll);
  }
  async clearAll(): Promise<void> {
    await this.http.delete(endpoints.me.notifications);
  }
  async registerPushToken(token: string, platform: 'ios' | 'android'): Promise<void> {
    await this.http.post(endpoints.me.pushTokens, { token, platform });
  }
  async removePushToken(token: string): Promise<void> {
    await this.http.delete(endpoints.me.pushToken(token));
  }
}

export class HttpContentRepository implements ContentRepository {
  constructor(private readonly http: HttpClient) {}
  async getOffers(): Promise<Offer[]> {
    return (await this.http.get<D.OfferDTO[]>(endpoints.offers, { auth: false })).map(M.offerFromDto);
  }
  async getFaqs(): Promise<Faq[]> {
    return this.http.get<D.FaqDTO[]>(endpoints.faqs, { auth: false });
  }
}
