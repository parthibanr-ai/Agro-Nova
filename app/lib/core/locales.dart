import 'package:flutter/cupertino.dart';
import 'package:flutter/material.dart';
import 'package:flutter_localizations/flutter_localizations.dart';

import '../l10n/app_localizations.dart';

/// Languages AgriN offers: 22 Indian scheduled languages + English + Brazil/Russia/China.
/// [uiFallback] is the language whose *static* UI strings are used until an ARB exists for the language
/// (run tools/translate_arb.py); dynamic content from the API is always requested in [code].
class AgriLanguage {
  const AgriLanguage(this.code, this.native, {this.uiFallback});
  final String code;
  final String native;
  final String? uiFallback;
}

const supportedAgriLanguages = <AgriLanguage>[
  AgriLanguage('en', 'English'),
  AgriLanguage('hi', 'हिन्दी'),
  AgriLanguage('as', 'অসমীয়া'),
  AgriLanguage('bn', 'বাংলা'),
  AgriLanguage('brx', 'बर\'', uiFallback: 'hi'),
  AgriLanguage('doi', 'डोगरी', uiFallback: 'hi'),
  AgriLanguage('gu', 'ગુજરાતી'),
  AgriLanguage('kn', 'ಕನ್ನಡ'),
  AgriLanguage('ks', 'کٲشُر', uiFallback: 'ur'),
  AgriLanguage('kok', 'कोंकणी', uiFallback: 'hi'),
  AgriLanguage('mai', 'मैथिली', uiFallback: 'hi'),
  AgriLanguage('ml', 'മലയാളം'),
  AgriLanguage('mni', 'মৈতৈলোন্', uiFallback: 'bn'),
  AgriLanguage('mr', 'मराठी'),
  AgriLanguage('ne', 'नेपाली'),
  AgriLanguage('or', 'ଓଡ଼ିଆ'),
  AgriLanguage('pa', 'ਪੰਜਾਬੀ'),
  AgriLanguage('sa', 'संस्कृतम्', uiFallback: 'hi'),
  AgriLanguage('sat', 'ᱥᱟᱱᱛᱟᱲᱤ', uiFallback: 'hi'),
  AgriLanguage('sd', 'سنڌي'),
  AgriLanguage('ta', 'தமிழ்'),
  AgriLanguage('te', 'తెలుగు'),
  AgriLanguage('ur', 'اردو'),
  AgriLanguage('pt-BR', 'Português (Brasil)'),
  AgriLanguage('ru', 'Русский'),
  AgriLanguage('zh-CN', '简体中文'),
];

/// Flutter [Locale] for a language code ('pt-BR' -> pt_BR).
Locale localeFor(String code) {
  final parts = code.split('-');
  return Locale(parts[0], parts.length > 1 ? parts[1] : null);
}

/// The locale whose ARB strings we actually have, walking the UI fallback chain.
Locale resolveUiLocale(String code) {
  final have = AppLocalizations.supportedLocales.map((l) => l.languageCode).toSet();
  var lang = supportedAgriLanguages.firstWhere((l) => l.code == code, orElse: () => supportedAgriLanguages.first);
  for (var i = 0; i < 3; i++) {
    final base = lang.code.split('-')[0];
    if (have.contains(base)) return Locale(base);
    final next = lang.uiFallback;
    if (next == null) break;
    lang = supportedAgriLanguages.firstWhere((l) => l.code == next);
  }
  return const Locale('en');
}

/// Flutter's built-in Material/Cupertino/Widgets localizations don't cover every Indian language
/// (e.g. Bodo, Santali). These delegates fall back to English chrome instead of crashing.
class _Fallback<T> extends LocalizationsDelegate<T> {
  const _Fallback(this.real, this.fallback);
  final LocalizationsDelegate<T> real;
  final Future<T> Function(Locale) fallback;

  @override
  bool isSupported(Locale locale) => true;

  @override
  Future<T> load(Locale locale) => real.isSupported(locale) ? real.load(locale) : fallback(locale);

  @override
  bool shouldReload(covariant LocalizationsDelegate<T> old) => false;
}

final localizationDelegates = <LocalizationsDelegate<dynamic>>[
  AppLocalizations.delegate,
  _Fallback<MaterialLocalizations>(GlobalMaterialLocalizations.delegate, DefaultMaterialLocalizations.load),
  _Fallback<WidgetsLocalizations>(GlobalWidgetsLocalizations.delegate, DefaultWidgetsLocalizations.load),
  _Fallback<CupertinoLocalizations>(GlobalCupertinoLocalizations.delegate, DefaultCupertinoLocalizations.load),
];

bool isRtl(String code) => const {'ur', 'sd', 'ks'}.contains(code);
