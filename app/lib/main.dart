import 'package:firebase_core/firebase_core.dart';
import 'package:firebase_app_check/firebase_app_check.dart';
import 'package:firebase_auth/firebase_auth.dart';
import 'package:firebase_messaging/firebase_messaging.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import 'core/api.dart';
import 'core/app_state.dart';
import 'core/locales.dart';
import 'l10n/app_localizations.dart';
import 'screens/home_screen.dart';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();

  // Firebase is optional in development: without google-services config the app falls back to the
  // backend's AUTH_MODE=dev header so it can still be run against a local server.
  var firebaseOn = false;
  var appCheckOn = false;
  try {
    await Firebase.initializeApp();
    firebaseOn = true;
    appCheckOn = await _activateAppCheck();
    if (FirebaseAuth.instance.currentUser == null) await FirebaseAuth.instance.signInAnonymously();
  } catch (_) {}

  final api = Api(
    // Dev login name; pick a seeded demo farmer with --dart-define=DEV_USER=farmer-br (see docs).
    devUser: firebaseOn ? null : const String.fromEnvironment('DEV_USER', defaultValue: 'dev-farmer'),
    tokenProvider: firebaseOn ? () async => FirebaseAuth.instance.currentUser?.getIdToken() : null,
    appCheckTokenProvider: appCheckOn ? () => FirebaseAppCheck.instance.getToken() : null,
  );
  final state = AppState(api);
  if (firebaseOn) {
    // After the server erased the account, leave that identity behind: the next request starts a fresh, empty one.
    state.onAccountDeleted = () async {
      await FirebaseAuth.instance.signOut();
      await FirebaseAuth.instance.signInAnonymously();
    };
  }
  runApp(ChangeNotifierProvider.value(value: state, child: const AgriNApp()));
  await state.init();

  if (firebaseOn) {
    // Push: subsidies, weather/El Nino alerts, value-addition and market nudges (FCM).
    try {
      await FirebaseMessaging.instance.requestPermission();
      final token = await FirebaseMessaging.instance.getToken();
      if (token != null) await api.post('/me/fcm-token', {'token': token});
    } catch (_) {}
  }
}

/// Turns on Firebase App Check so the server can tell the real app from a script. Debug builds use the debug
/// provider (register the token it prints in the Firebase console); release builds use Play Integrity / App Attest;
/// the web build needs --dart-define=RECAPTCHA_SITE_KEY=... Returns false (and the app still works, the server
/// decides what to do with an unverified request) if it cannot be activated.
Future<bool> _activateAppCheck() async {
  const siteKey = String.fromEnvironment('RECAPTCHA_SITE_KEY');
  if (kIsWeb && siteKey.isEmpty) return false;
  try {
    await FirebaseAppCheck.instance.activate(
      androidProvider: kDebugMode ? AndroidProvider.debug : AndroidProvider.playIntegrity,
      appleProvider: kDebugMode ? AppleProvider.debug : AppleProvider.appAttestWithDeviceCheckFallback,
      webProvider: kIsWeb ? ReCaptchaV3Provider(siteKey) : null,
    );
    return true;
  } catch (_) {
    return false;
  }
}

class AgriNApp extends StatelessWidget {
  const AgriNApp({super.key});

  @override
  Widget build(BuildContext context) {
    final lang = context.select<AppState, String>((s) => s.language);
    return MaterialApp(
      onGenerateTitle: (c) => AppLocalizations.of(c).appTitle,
      debugShowCheckedModeBanner: false,
      theme: ThemeData(colorSchemeSeed: const Color(0xFF2E7D32), useMaterial3: true),
      locale: resolveUiLocale(lang),
      supportedLocales: AppLocalizations.supportedLocales,
      localizationsDelegates: localizationDelegates,
      builder: (context, child) =>
          Directionality(textDirection: isRtl(lang) ? TextDirection.rtl : TextDirection.ltr, child: child!),
      home: const HomeScreen(),
    );
  }
}
