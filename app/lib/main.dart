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
import 'core/offline.dart';
import 'core/push_links.dart';
import 'l10n/app_localizations.dart';
import 'screens/consent_screen.dart';
import 'screens/home_screen.dart';
import 'screens/push_router.dart';
import 'widgets.dart';

final navigatorKey = GlobalKey<NavigatorState>();
final messengerKey = GlobalKey<ScaffoldMessengerState>();

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

  // What the phone remembers for use without a connection: recent answers, and changes waiting to be sent.
  final store = await OfflineStore.open();
  final queue = await WriteQueue.open();

  final api = Api(
    store: store,
    // Dev login name; pick a seeded demo farmer with --dart-define=DEV_USER=farmer-br (see docs).
    devUser: firebaseOn ? null : const String.fromEnvironment('DEV_USER', defaultValue: 'dev-farmer'),
    tokenProvider: firebaseOn ? () async => FirebaseAuth.instance.currentUser?.getIdToken() : null,
    appCheckTokenProvider: appCheckOn ? () => FirebaseAppCheck.instance.getToken() : null,
  );
  final state = AppState(api, queue: queue);
  if (firebaseOn) {
    // After the server erased the account, leave that identity behind: the next request starts a fresh, empty one.
    state.onAccountDeleted = () async {
      await FirebaseAuth.instance.signOut();
      await FirebaseAuth.instance.signInAnonymously();
    };
  }
  if (firebaseOn) {
    // Push (answers ready, subsidies, weather/El Nino alerts, value-addition and market nudges) is only switched on
    // once the farmer has agreed to notifications on the privacy screen; the state calls this then.
    state.onNotificationsAllowed = () async {
      await FirebaseMessaging.instance.requestPermission();
      final token = await FirebaseMessaging.instance.getToken();
      if (token != null) await api.post('/me/fcm-token', {'token': token});
    };
  }
  runApp(ChangeNotifierProvider.value(value: state, child: const AgriNApp()));
  await state.init();

  if (firebaseOn) _listenForPushTaps(state);
}

/// Opens the finished answer when the farmer taps a "ready" notification: from the background, from a closed app,
/// or from the message shown while the app is open.
void _listenForPushTaps(AppState state) {
  void open(PushLink link) {
    if (state.needsConsent) return; // the privacy notice comes first
    final page = screenForPush(link, state.plots);
    if (page != null) navigatorKey.currentState?.push(MaterialPageRoute(builder: (_) => page));
  }

  try {
    FirebaseMessaging.onMessageOpenedApp.listen((m) {
      final link = PushLink.fromData(m.data);
      if (link != null) open(link);
    });
    FirebaseMessaging.instance.getInitialMessage().then((m) {
      final link = m == null ? null : PushLink.fromData(m.data);
      if (link != null) open(link);
    });
    FirebaseMessaging.onMessage.listen((m) {
      final link = PushLink.fromData(m.data);
      final ctx = navigatorKey.currentContext;
      if (link == null || ctx == null || !ctx.mounted) return;
      messengerKey.currentState?.showSnackBar(SnackBar(
        content: Text(m.notification?.title ?? ''),
        action: SnackBarAction(label: AppLocalizations.of(ctx).openAnswer, onPressed: () => open(link)),
      ));
    });
  } catch (_) {} // no push in this environment; the app works without it
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
      navigatorKey: navigatorKey,
      scaffoldMessengerKey: messengerKey,
      debugShowCheckedModeBanner: false,
      theme: ThemeData(colorSchemeSeed: const Color(0xFF2E7D32), useMaterial3: true),
      locale: resolveUiLocale(lang),
      supportedLocales: AppLocalizations.supportedLocales,
      localizationsDelegates: localizationDelegates,
      builder: (context, child) => Directionality(
        textDirection: isRtl(lang) ? TextDirection.rtl : TextDirection.ltr,
        child: Column(children: [Expanded(child: child!), const ConnectivityBanner()]),
      ),
      home: const _Gate(),
    );
  }
}

/// The first thing on screen: nothing until the app knows whether the farmer has agreed to the privacy notice, then
/// the notice or the home screen. If the server later asks for consent again (the notice changed), whatever screen is
/// open is closed so the notice shows.
class _Gate extends StatefulWidget {
  const _Gate();
  @override
  State<_Gate> createState() => _GateState();
}

class _GateState extends State<_Gate> {
  late final AppState _state = context.read<AppState>();
  late bool _needed = _state.needsConsent;

  @override
  void initState() {
    super.initState();
    _state.addListener(_changed);
  }

  @override
  void dispose() {
    _state.removeListener(_changed);
    super.dispose();
  }

  void _changed() {
    if (_state.needsConsent && !_needed) Navigator.of(context).popUntil((r) => r.isFirst);
    _needed = _state.needsConsent;
  }

  @override
  Widget build(BuildContext context) {
    final s = context.watch<AppState>();
    if (!s.ready) return const Scaffold(body: Center(child: CircularProgressIndicator()));
    return s.needsConsent ? const ConsentScreen() : const HomeScreen();
  }
}
