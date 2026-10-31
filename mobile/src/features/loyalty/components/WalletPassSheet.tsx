import { Image } from 'expo-image';
import { usePathname } from 'expo-router';
import { StyleSheet, View } from 'react-native';
import { useSafeAreaInsets } from 'react-native-safe-area-context';
import { BottomSheet } from '@/components/layout/BottomSheet';
import { Barcode } from '@/components/ui/Barcode';
import { Text } from '@/components/ui/Text';
import { Touchable } from '@/components/ui/Touchable';
import { cardBalance } from '@/domain/loyalty';
import { useProfile } from '@/features/profile/hooks';
import { errorMessage } from '@/services/errors';
import { walletPassService } from '@/services/integrations/wallet';
import { showToast, useUiStore } from '@/store/ui';
import { colors, creamA, eyebrow, fontFamilies, serif, shadows, sans } from '@/theme';
import { useAddWalletPass, useLoyaltyCard } from '../hooks';

const LOGO = require('../../../../assets/branding/logo-stacked-white.webp');

/**
 * Apple Wallet "Add pass" sheet styled like the iOS add-pass card.
 * MOCK: "Add" records the pass as added. Real API: "Add" opens the server's add-to-Wallet page (see services/integrations/wallet.ts).
 */
export function WalletPassSheet() {
  const open = useUiStore((s) => s.walletOpen);
  const setOpen = useUiStore((s) => s.setWalletOpen);
  const insets = useSafeAreaInsets();
  const { data: card } = useLoyaltyCard();
  const { data: profile } = useProfile();
  const add = useAddWalletPass();
  const added = !!card?.walletPassAdded;
  const { shown: stamps, per } = cardBalance(card);

  const pathname = usePathname();
  const addToWallet = () =>
    add.mutate(pathname, {
      onSuccess: (next) => {
        setOpen(false);
        if (walletPassService.isDemo) showToast('Wallet preview — no pass was installed');
        else if (next.walletPassAdded) showToast('Your coffee card is in Apple Wallet');
      },
      onError: (e) => showToast(errorMessage(e)),
    });
  // Once the card is in Wallet, "Done" just closes; "Add again" (below) re-adds a removed pass.
  const onAction = () => (added ? setOpen(false) : addToWallet());

  return (
    <BottomSheet visible={open} onClose={() => setOpen(false)} accessibilityLabel="Apple Wallet pass" backgroundColor={colors.walletSheet} backdropColor="rgba(0,0,0,0.5)">
      <View style={styles.bar}>
        <Touchable onPress={() => setOpen(false)} pressedScale={1} hitSlop={8}>
          <Text style={[styles.sys, { fontSize: 16, color: colors.iosBlue }]}>Cancel</Text>
        </Touchable>
        <Text style={[styles.sys, { fontSize: 16, fontWeight: '600', color: colors.white }]}>{added ? 'Coffee card' : 'Add pass'}</Text>
        <Touchable onPress={onAction} pressedScale={1} hitSlop={8} disabled={add.isPending}>
          <Text style={[styles.sys, { fontSize: 16, fontWeight: '600', color: colors.iosBlue, opacity: add.isPending ? 0.5 : 1 }]}>{added ? 'Done' : 'Add'}</Text>
        </Touchable>
      </View>
      <View style={[styles.body, { paddingBottom: Math.max(34, insets.bottom + 12) }]}>
        <View style={styles.pass}>
          <View style={styles.passTop}>
            <Image source={LOGO} style={styles.logo} contentFit="contain" accessibilityLabel="eighty twenty" />
            <Text style={[eyebrow(9, 0.2), { color: colors.sand }]}>Coffee card</Text>
          </View>
          <View style={styles.passMid}>
            <View style={styles.count}>
              <Text style={[serif(52, 300, undefined, -0.03), { color: colors.creamText }]}>{stamps}</Text>
              <Text style={[serif(18, 300), { color: colors.sand }]}>/{per}</Text>
            </View>
            <Text style={[sans(11, 400, 1.4), { color: creamA(0.75), textAlign: 'right' }]}>{profile?.name}</Text>
          </View>
          <View style={styles.cells}>
            {Array.from({ length: per }, (_, i) => (
              <View key={i} style={[styles.cell, i < stamps ? { backgroundColor: colors.sand, borderColor: colors.sand } : { borderColor: creamA(0.35) }]} />
            ))}
          </View>
          <View style={styles.barcode}>
            <Barcode value={card?.memberNumber} height={44} color={colors.walletInk} />
          </View>
        </View>
        <Text style={[styles.sys, styles.foot]}>
          {added ? 'In your Wallet — it updates automatically as you collect stamps.' : 'Your coffee card appears in Wallet and updates automatically as you collect stamps.'}
        </Text>
        {walletPassService.isDemo ? <Text style={[styles.sys, styles.foot]}>Preview — Apple Wallet passes aren’t issued yet.</Text> : null}
        {added && !walletPassService.isDemo ? (
          <Touchable onPress={addToWallet} pressedScale={0.97} disabled={add.isPending} style={styles.again}>
            <Text style={[styles.sys, { fontSize: 14, fontWeight: '600', color: colors.iosBlue, opacity: add.isPending ? 0.5 : 1 }]}>Add to Apple Wallet again</Text>
          </Touchable>
        ) : null}
      </View>
    </BottomSheet>
  );
}

const styles = StyleSheet.create({
  sys: { fontFamily: fontFamilies.system },
  bar: { flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between', paddingVertical: 16, paddingHorizontal: 18 },
  body: { paddingTop: 6, paddingHorizontal: 18 },
  pass: { padding: 18, borderRadius: 14, backgroundColor: colors.espresso, ...shadows.walletPass },
  passTop: { flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between', gap: 12 },
  logo: { height: 30, width: 42 },
  passMid: { flexDirection: 'row', alignItems: 'flex-end', justifyContent: 'space-between', gap: 12, marginTop: 18 },
  count: { flexDirection: 'row', alignItems: 'baseline', gap: 4 },
  cells: { flexDirection: 'row', gap: 4, marginTop: 14 },
  cell: { flex: 1, aspectRatio: 1, borderRadius: 999, borderWidth: 1 },
  barcode: { marginTop: 16, paddingVertical: 10, paddingHorizontal: 14, borderRadius: 8, backgroundColor: colors.creamText },
  again: { marginTop: 14, minHeight: 44, alignItems: 'center', justifyContent: 'center' },
  foot: { marginTop: 16, marginHorizontal: 4, fontSize: 12, lineHeight: 18, color: 'rgba(255,255,255,0.6)' },
});
