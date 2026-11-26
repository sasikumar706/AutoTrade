import { createBottomTabNavigator } from "@react-navigation/bottom-tabs";
import { DefaultTheme, NavigationContainer } from "@react-navigation/native";
import { createNativeStackNavigator } from "@react-navigation/native-stack";
import { StatusBar } from "expo-status-bar";
import * as WebBrowser from "expo-web-browser";
import React from "react";
import { Pressable, Text, View, Platform } from "react-native";
import { SafeAreaProvider, useSafeAreaInsets } from "react-native-safe-area-context";
import AiScreen from "./src/screens/AiScreen";
import AutoTradeScreen, { AutoDetailScreen } from "./src/screens/AutoTradeScreen";
import OptionsResultScreen from "./src/screens/OptionsResultScreen";
import PositionScreen from "./src/screens/PositionScreen";
import { TradeScreen } from "./src/screens/TradeTabs";
import BacktestResultScreen from "./src/screens/BacktestResultScreen";
import HomeScreen from "./src/screens/HomeScreen";
import MarketScreen from "./src/screens/MarketScreen";
import QuoteScreen from "./src/screens/QuoteScreen";
import SessionScreen from "./src/screens/SessionScreen";
import SettingsScreen from "./src/screens/SettingsScreen";
import ZerodhaScreen from "./src/screens/ZerodhaScreen";
import SwingScreen from "./src/screens/SwingScreen";
import { MODE_INFO, ModeChip, ModeProvider, useMode } from "./src/mode";
import AutoHomeScreen from "./src/screens/AutoHomeScreen";
import HistoryScreen from "./src/screens/HistoryScreen";
import { C } from "./src/ui";
import { CrashGuard, installGlobalErrorHandler } from "./src/CrashGuard";

installGlobalErrorHandler();

WebBrowser.maybeCompleteAuthSession();

const Tab = createBottomTabNavigator();
const Stack = createNativeStackNavigator();

const theme = {
  ...DefaultTheme,
  colors: { ...DefaultTheme.colors, background: C.bg, card: C.card, border: C.border, primary: C.blue, text: C.text },
};

const icon = (glyph: string) => ({ color, focused }: { color: string; focused: boolean }) => (
  <View style={{ minWidth: 44, height: 26, alignItems: "center", justifyContent: "center", borderRadius: 13,
    backgroundColor: focused ? color + "1F" : "transparent" }}>
    <Text style={{ color, fontSize: 16, lineHeight: 20, includeFontPadding: false }}>{glyph}</Text>
  </View>
);

/** Bottom tabs. Paper/Live and Auto/Manual are set in Settings (the header chip opens it). */
function Tabs() {
  const { mode, auto } = useMode();
  const info = MODE_INFO[mode];
  const insets = useSafeAreaInsets();
  // some phones report 0 for the navigation bar in edge-to-edge mode -> always keep at least the 3-button bar height
  const bottom = Platform.OS === "android" ? Math.max(insets.bottom, 48) : insets.bottom;
  return (
    <Tab.Navigator safeAreaInsets={{ bottom }} screenOptions={({ navigation }) => ({
      tabBarActiveTintColor: info.color, tabBarInactiveTintColor: C.dim,
      tabBarStyle: { backgroundColor: C.card, borderTopColor: C.border, paddingTop: 4 },
      tabBarLabelStyle: { fontSize: 12, fontWeight: "700" },
      headerStyle: { backgroundColor: C.card }, headerShadowVisible: false,
      headerTitleStyle: { color: C.ink, fontWeight: "800", fontSize: 19 },
      headerRight: () => (
        <View style={{ flexDirection: "row", alignItems: "center", gap: 14, paddingRight: 14 }}>
          <ModeChip onPress={() => navigation.navigate("Settings")} />
          <Pressable onPress={() => navigation.navigate("Settings")} hitSlop={12}>
            <Text style={{ color: C.dim, fontSize: 20 }}>⚙</Text>
          </Pressable>
        </View>
      ),
    })}>
      <Tab.Screen name="Home" component={HomeScreen} options={{ title: "Today", tabBarLabel: "Home", tabBarIcon: icon("🏠") }} />
      <Tab.Screen name="Auto" component={AutoHomeScreen}
        options={{ title: auto ? "Auto · on" : "Auto · off", tabBarLabel: "Auto", tabBarIcon: icon("⚡") }} />
      <Tab.Screen name="Trade" component={TradeScreen}
        options={{ title: `${info.label} trading`, tabBarLabel: info.label, tabBarIcon: icon(info.icon) }} />
      <Tab.Screen name="Market" component={MarketScreen} options={{ title: "Market", tabBarIcon: icon("📊") }} />
      <Tab.Screen name="History" component={HistoryScreen} options={{ title: "History", tabBarIcon: icon("🕘") }} />
    </Tab.Navigator>
  );
}

function Root() {
  return (
    <NavigationContainer theme={theme}>
      <StatusBar style="dark" />
      <Stack.Navigator screenOptions={{ headerStyle: { backgroundColor: C.card }, headerShadowVisible: false,
        headerTitleStyle: { color: C.ink, fontWeight: "800" }, headerTintColor: C.blue, contentStyle: { backgroundColor: C.bg } }}>
        <Stack.Screen name="Tabs" component={Tabs} options={{ headerShown: false }} />
        <Stack.Screen name="BacktestResult" component={BacktestResultScreen} options={{ title: "Backtest result" }} />
        <Stack.Screen name="Session" component={SessionScreen} options={{ title: "Session" }} />
        <Stack.Screen name="Settings" component={SettingsScreen} options={{ title: "Settings" }} />
        <Stack.Screen name="OptionsResult" component={OptionsResultScreen} options={{ title: "Options backtest" }} />
        <Stack.Screen name="Position" component={PositionScreen} options={{ title: "Position" }} />
        <Stack.Screen name="Finder" component={AiScreen} options={{ title: "AI Strategy Finder" }} />
        <Stack.Screen name="Quote" component={QuoteScreen} options={{ title: "Quote" }} />
        <Stack.Screen name="AutoAdvanced" component={AutoTradeScreen} options={{ title: "My own strategies" }} />
        <Stack.Screen name="AutoDetail" component={AutoDetailScreen} options={{ title: "Auto Trade" }} />
        <Stack.Screen name="Zerodha" component={ZerodhaScreen} options={{ title: "Zerodha account" }} />
        <Stack.Screen name="Swing" component={SwingScreen} options={{ title: "Swing trend" }} />
      </Stack.Navigator>
    </NavigationContainer>
  );
}

export default function App() {
  return (
    <SafeAreaProvider>
      <CrashGuard>
      <ModeProvider>
        <Root />
      </ModeProvider>
      </CrashGuard>
    </SafeAreaProvider>
  );
}
