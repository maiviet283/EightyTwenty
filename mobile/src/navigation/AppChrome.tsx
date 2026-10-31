import { useCallback, useEffect, useRef, type ReactNode } from 'react';
import { StyleSheet, View } from 'react-native';
import { TabBar, useTabBarHeight } from '@/components/layout/TabBar';
import { PaidOverlay } from '@/components/shared/PaidOverlay';
import { BiometricLock } from '@/features/auth/components/BiometricLock';
import { ToastHost } from '@/components/shared/ToastHost';
import { MiniCart } from '@/features/cart/components/MiniCart';
import { OrderSettingsSheet } from '@/features/cart/components/OrderSettingsSheet';
import { CafePickerSheet } from '@/features/locations/components/CafePickerSheet';
import { useLocations, useSelectedLocation } from '@/features/locations/hooks';
import { MemberCardSheet } from '@/features/loyalty/components/MemberCardSheet';
import { usePushNotifications } from '@/features/notifications/usePushNotifications';
import { WalletPassSheet } from '@/features/loyalty/components/WalletPassSheet';
import { ItemSheet } from '@/features/menu/components/ItemSheet';
import { AvatarSheet } from '@/features/profile/components/AvatarSheet';
import { usePreferences } from '@/features/profile/hooks';
import { DEFAULT_LOCATION_ID, useCartCount, useCartStore } from '@/store/cart';
import { useUiStore, type TabKey } from '@/store/ui';
import { activeTab, metaFor, TAB_ROUTES } from './routeMeta';
import { TabNavigationContext } from './tabNavigation';

interface AppChromeProps {
  /** The stack's own state: SDK 58 only accepts RESET with a complete state (key, routeNames, routeKeySeq). */
  state: { routes: { name: string }[] };
  navigation: { reset(state: object): void };
  children: ReactNode;
}

/**
 * Everything drawn over the app stack: tab bar, mini-cart, global sheets, toasts and the
 * payment overlay. Rendered through the Stack `layout` prop so it can reset history.
 */
export function AppChrome({ state, navigation, children }: AppChromeProps) {
  const routeNames = state.routes.map((r) => r.name);
  const top = routeNames[routeNames.length - 1];
  const meta = metaFor(top);
  const tab = activeTab(routeNames);
  const tabBarHeight = useTabBarHeight();
  const cartCount = useCartCount();
  const itemOpen = useUiStore((s) => !!s.item);
  const location = useSelectedLocation();
  useSyncHomeCafe();
  usePushNotifications();

  /** Jump to a tab root, clearing history (prototype `tabGo`). */
  const goToTab = useCallback(
    (key: TabKey) => {
      useUiStore.setState({ item: null, cafePicker: null, orderSettingsOpen: false, walletOpen: false, memberCardOpen: false, avatarOpen: false });
      // Routes without a key get one minted by the router.
      navigation.reset({ ...state, index: 0, routes: [{ name: TAB_ROUTES[key] }], stale: false });
    },
    [navigation, state],
  );

  const onTabPress = useCallback(
    (key: TabKey) => {
      if (top === TAB_ROUTES[key]) useUiStore.getState().reselectTab(key);
      else goToTab(key);
    },
    [goToTab, top],
  );

  return (
    <View style={styles.root}>
      <TabNavigationContext.Provider value={goToTab}>{children}</TabNavigationContext.Provider>
      {meta.mini && cartCount > 0 && !itemOpen ? <MiniCart bottom={tabBarHeight + 8} locationName={location?.name ?? ''} /> : null}
      <TabBar active={tab} visible={meta.tabs} onPress={onTabPress} />
      <ItemSheet />
      <CafePickerSheet />
      <OrderSettingsSheet />
      <WalletPassSheet />
      <MemberCardSheet />
      <AvatarSheet />
      <PaidOverlay />
      <ToastHost />
      <BiometricLock />
    </View>
  );
}

/** After sign-in, start ordering from the customer's saved home café. */
function useSyncHomeCafe() {
  const { data: prefs } = usePreferences();
  const { data: locations } = useLocations();
  const synced = useRef(false);
  useEffect(() => {
    if (synced.current || !prefs || !locations) return;
    const cart = useCartStore.getState();
    const home = locations.find((l) => l.id === prefs.homeCafeId);
    if (home) cart.setLocation(home.id, home.methods);
    else if (!locations.some((l) => l.id === cart.context.locationId)) {
      // A café that is no longer public (cached pick or saved home café): fall back to the default.
      const fallback = locations.find((l) => l.id === DEFAULT_LOCATION_ID) ?? locations[0];
      if (fallback) cart.setLocation(fallback.id, fallback.methods);
    }
    synced.current = true;
  }, [prefs, locations]);
}

// overflow hidden: the tab bar slides 24pt below the edge when hidden; never let that scroll the page (web).
const styles = StyleSheet.create({ root: { flex: 1, overflow: 'hidden' } });
