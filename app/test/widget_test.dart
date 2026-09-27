import 'package:agrin/core/locales.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  test('offers 22 Indian languages plus English, Portuguese, Russian and Chinese', () {
    expect(supportedAgriLanguages.length, 26);
    expect(supportedAgriLanguages.map((l) => l.code), containsAll(['en', 'hi', 'ta', 'pt-BR', 'ru', 'zh-CN']));
  });

  test('maps region-tagged codes to Flutter locales', () {
    expect(localeFor('pt-BR').countryCode, 'BR');
    expect(localeFor('hi').languageCode, 'hi');
  });

  test('right-to-left languages are flagged', () {
    expect(isRtl('ur'), isTrue);
    expect(isRtl('hi'), isFalse);
  });
}
