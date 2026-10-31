import { Image } from 'expo-image';
import { useRef, useState } from 'react';
import { StyleSheet, View, type ScrollViewInstance } from 'react-native';
import { KeyboardAwareScrollView } from '@/components/layout/KeyboardAwareScrollView';
import { Rise } from '@/components/layout/Rise';
import { Screen } from '@/components/layout/Screen';
import { ScreenHeader } from '@/components/layout/ScreenHeader';
import { AnimatedCheck } from '@/components/shared/AnimatedCheck';
import { Button } from '@/components/ui/Button';
import { Chip } from '@/components/ui/Chip';
import { Text } from '@/components/ui/Text';
import { TextField } from '@/components/ui/TextField';
import { GIFT_CARD_AMOUNTS } from '@/constants/business';
import { config } from '@/constants/config';
import { useBottomSpace } from '@/hooks/useBottomSpace';
import { useNow } from '@/hooks/useNow';
import { errorMessage } from '@/services/errors';
import { paymentProvider } from '@/services/integrations/payments';
import { creamA, colors, eyebrow, sans, serif, shadows } from '@/theme';
import type { GiftCardActivity } from '@/types';
import { formatRelativeShort } from '@/utils/datetime';
import { formatMoney } from '@/utils/format';
import { isEmail } from '@/utils/validation';
import { useGiftCard, useGiftCardActivity, useSendGiftCard } from '../hooks';

const LOGO = require('../../../../assets/branding/logo-stacked-white.webp');

function amountLabel(a: GiftCardActivity): string {
  if (a.kind === 'added') return '+' + formatMoney(a.amount);
  if (a.kind === 'spent') return '−' + formatMoney(a.amount);
  return formatMoney(a.amount);
}

/**
 * Gift card balance + "Send a gift card" (eGift via Square — DEMO: Apple Pay is simulated,
 * no payment is taken and no eGift is issued).
 */
export function GiftCardsScreen() {
  const scroll = useRef<ScrollViewInstance>(null);
  const now = useNow();
  const bottom = useBottomSpace(132);
  const { data: card } = useGiftCard();
  const { data: activity = [] } = useGiftCardActivity();
  const send = useSendGiftCard();
  const [amount, setAmount] = useState<number>(50);
  const [to, setTo] = useState('');
  const [email, setEmail] = useState('');
  const [msg, setMsg] = useState('');
  const [error, setError] = useState('');
  const [sent, setSent] = useState<{ to: string; email: string } | null>(null);

  const submit = async () => {
    if (!to.trim() || !isEmail(email)) return setError('Add their name and a valid email.');
    try {
      setError('');
      await send.mutateAsync({ amount, recipientName: to.trim(), recipientEmail: email.trim(), message: msg.trim() });
      setSent({ to: to.trim(), email: email.trim() });
      scroll.current?.scrollTo({ y: 0, animated: true });
    } catch (e) {
      setError(errorMessage(e));
    }
  };

  return (
    <Screen>
      <ScreenHeader title="Gift cards" />
      <KeyboardAwareScrollView ref={scroll} contentContainerStyle={{ paddingBottom: bottom }}>
        <View style={styles.card} accessible accessibilityLabel={`Gift card balance ${formatMoney(card?.balance ?? 0)}, card ending ${card?.last4 ?? ''}`}>
          <View style={styles.ring} />
          <View style={styles.cardTop}>
            <Image source={LOGO} style={styles.logo} contentFit="contain" accessibilityLabel="eighty twenty" />
            <Text style={[eyebrow(10, 0.14), { color: colors.sand }]}>Gift card</Text>
          </View>
          <Text style={[eyebrow(10, 0.12), styles.balanceLabel]}>Balance</Text>
          <Text style={[serif(46, 300, undefined, -0.03), styles.balance]}>{formatMoney(card?.balance ?? 0)}</Text>
          <Text style={[sans(12, 400, 1.4), styles.cardMeta]}>Card ·· {card?.last4} · redeemable at any EightyTwenty café</Text>
        </View>

        {!config.features.giftCardSales ? (
          // Balance and history only until eGift cards can be bought in the app.
          <Text style={[sans(13, 400, 1.5), styles.inStore]}>Gift cards are available at any EightyTwenty café and can be used here and in store.</Text>
        ) : sent ? (
          <Rise style={styles.sent}>
            <AnimatedCheck size={64} color={colors.success} />
            <Text accessibilityRole="header" style={[serif(32, 400, 1.05, -0.015), styles.sentTitle]}>
              ${amount} gift sent.
            </Text>
            <Text style={[sans(14, 400, 1.5), styles.sentLine]}>
              On its way to {sent.to}
              {sent.email ? ` at ${sent.email}` : ''}.
            </Text>
            <Button
              label="Send another"
              variant="outline"
              height={46}
              onPress={() => {
                setSent(null);
                setTo('');
                setEmail('');
                setMsg('');
              }}
              style={styles.another}
            />
          </Rise>
        ) : (
          <View style={styles.section}>
            <Text accessibilityRole="header" style={[sans(17, 600, 1.25, -0.01), { color: colors.ink }]}>
              Send a gift card
            </Text>
            <Text style={[sans(13, 400, 1.5), styles.lede]}>Treat a friend who loves our food and coffee to the gift of delicious food.</Text>
            <View style={styles.amounts}>
              {GIFT_CARD_AMOUNTS.map((a) => (
                <Chip key={a} label={`$${a}`} selected={amount === a} onPress={() => setAmount(a)} height={48} radius={12} fontSize={15} weight={600} style={styles.amount} />
              ))}
            </View>
            <View style={styles.inputs}>
              <TextField value={to} onChangeText={(t) => (setTo(t), setError(''))} placeholder="Their name" accessibilityLabel="Recipient name" />
              <TextField value={email} onChangeText={(t) => (setEmail(t), setError(''))} placeholder="Their email" accessibilityLabel="Recipient email" keyboardType="email-address" autoCapitalize="none" />
              <TextField value={msg} onChangeText={setMsg} placeholder="A short message (optional)" accessibilityLabel="Message" multiline height={70} style={sans(14, 400, 1.45)} />
            </View>
            {error ? (
              <Text accessibilityRole="alert" style={[sans(13, 500, 1.4), styles.error]}>
                {error}
              </Text>
            ) : null}
            <Button label={`Pay $${amount} with Apple Pay`} variant="applePay" systemFont onPress={submit} loading={send.isPending} height={54} radius={14} fontSize={15} style={styles.pay} />
            <Text style={[sans(12, 400, 1.45), styles.note]}>eGift cards are issued and redeemed through Square. Physical cards are available in store.</Text>
            {paymentProvider.isDemo ? <Text style={[sans(12, 400, 1.45), styles.note]}>Demo mode — no payment is taken and no eGift card is sent.</Text> : null}
          </View>
        )}

        <View style={styles.section}>
          <Text accessibilityRole="header" style={[sans(17, 600, 1.25, -0.01), { color: colors.ink }]}>
            History
          </Text>
          <View style={styles.history}>
            {activity.map((a) => (
              <View key={a.id} style={styles.historyRow}>
                <View>
                  <Text style={[sans(14, 500, 1.3), { color: colors.ink }]}>{a.description}</Text>
                  <Text style={[sans(12, 400, 1.3), styles.when]}>{formatRelativeShort(a.occurredAt, now)}</Text>
                </View>
                <Text style={[sans(14, 600), { color: colors.ink }]}>{amountLabel(a)}</Text>
              </View>
            ))}
          </View>
          <Text style={[sans(12, 400, 1.5), styles.terms]}>
            Gift cards can be used at any EightyTwenty café. They can’t be exchanged for cash or reloaded, and lost or stolen cards can’t be replaced, except where the Australian Consumer Law requires otherwise.
          </Text>
        </View>
      </KeyboardAwareScrollView>
    </Screen>
  );
}

const styles = StyleSheet.create({
  card: { marginTop: 8, marginHorizontal: 20, padding: 20, borderRadius: 18, overflow: 'hidden', backgroundColor: colors.espresso, ...shadows.giftCard },
  ring: { position: 'absolute', right: -60, top: -60, width: 200, height: 200, borderRadius: 100, borderWidth: 1, borderColor: 'rgba(233,200,158,0.14)' },
  cardTop: { flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between' },
  logo: { height: 30, width: 42 },
  balanceLabel: { marginTop: 28, color: creamA(0.7) },
  balance: { marginTop: 8, color: colors.creamText },
  cardMeta: { marginTop: 14, color: creamA(0.75) },
  section: { paddingTop: 28, paddingHorizontal: 20 },
  inStore: { paddingTop: 18, paddingHorizontal: 20, color: colors.textSecondary },
  lede: { marginTop: 6, color: colors.textSecondary },
  amounts: { flexDirection: 'row', gap: 8, marginTop: 14 },
  amount: { flex: 1, paddingHorizontal: 0 },
  inputs: { gap: 8, marginTop: 12 },
  error: { marginTop: 10, color: colors.danger },
  pay: { marginTop: 12 },
  note: { marginTop: 10, color: colors.textMuted },
  sent: { alignItems: 'center', gap: 8, paddingTop: 28, paddingHorizontal: 24 },
  sentTitle: { marginTop: 10, color: colors.ink, textAlign: 'center' },
  sentLine: { maxWidth: 280, color: colors.textSecondary, textAlign: 'center' },
  another: { marginTop: 10, paddingHorizontal: 18 },
  history: { marginTop: 6 },
  historyRow: { flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between', gap: 12, paddingVertical: 12 },
  when: { marginTop: 2, color: colors.textSecondary },
  terms: { marginTop: 16, color: colors.textMuted },
});
