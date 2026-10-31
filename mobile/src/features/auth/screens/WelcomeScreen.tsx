import { Image } from 'expo-image';
import { StatusBar } from 'expo-status-bar';
import { useEffect, useState } from 'react';
import { StyleSheet, View } from 'react-native';
import { useSafeAreaInsets } from 'react-native-safe-area-context';
import { FadeIn } from '@/components/layout/Rise';
import { ToastHost } from '@/components/shared/ToastHost';
import { Button } from '@/components/ui/Button';
import { Text } from '@/components/ui/Text';
import { useSessionStore } from '@/store/session';
import { showToast } from '@/store/ui';
import { colors, inkA, serifItalic } from '@/theme';
import { AuthSheet } from '../components/AuthSheet';
import { FoodOrbitHeadline, FoodOrbitStage, useFoodOrbit } from '../components/FoodOrbit';
import type { AuthMode } from '../validation';

const LOGO = require('../../../../assets/branding/logo-stacked-brown.webp');

/** Welcome: wordmark, tagline, rotating food hero, Log in / Sign up. */
export function WelcomeScreen() {
  const insets = useSafeAreaInsets();
  const [mode, setMode] = useState<AuthMode | null>(null);
  const { pos, layers } = useFoodOrbit();
  const expired = useSessionStore((s) => s.expired);

  useEffect(() => {
    if (expired) showToast('Your session has expired. Please log in again.');
  }, [expired]);

  return (
    <FadeIn duration={520} style={styles.root}>
      <StatusBar style="dark" />
      <View style={[styles.top, { paddingTop: insets.top + 8 }]}>
        <Wordmark />
        <Text style={[serifItalic(21, 400, 1.2), styles.tagline]}>Wholesome food, great coffee.</Text>
        <FoodOrbitHeadline layers={layers} />
        <FoodOrbitStage pos={pos} />
      </View>
      <View style={[styles.actions, { paddingBottom: Math.max(30, insets.bottom + 12) }]}>
        <Button label="Log in" onPress={() => setMode('login')} height={52} radius={10} weight={500} fontSize={15} />
        <Button label="Sign up" onPress={() => setMode('signup')} variant="outline" height={52} radius={10} weight={500} fontSize={15} style={styles.signup} />
      </View>
      <AuthSheet mode={mode} onModeChange={setMode} onClose={() => setMode(null)} />
      <ToastHost />
    </FadeIn>
  );
}

/** The official stacked "eighty twenty" logo, the same artwork as the website and BOS. */
export function Wordmark() {
  return (
    <View style={styles.wordmark} accessibilityRole="header" accessibilityLabel="EightyTwenty">
      <Image source={LOGO} style={styles.logo} contentFit="contain" />
    </View>
  );
}

const styles = StyleSheet.create({
  root: { flex: 1, backgroundColor: colors.creamWarm, overflow: 'hidden' },
  top: { flex: 1, minHeight: 0, paddingHorizontal: 16, alignItems: 'center' },
  wordmark: { alignItems: 'center' },
  logo: { width: 96, height: 68 },
  tagline: { marginTop: 24, color: colors.brandRed, textAlign: 'center' },
  actions: { zIndex: 40, gap: 10, paddingTop: 40, paddingHorizontal: 20, backgroundColor: colors.creamWarm },
  signup: { borderColor: inkA(0.28) },
});
