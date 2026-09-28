import 'dart:convert';

import 'package:agrin/core/api.dart';
import 'package:agrin/core/app_state.dart';
import 'package:agrin/core/locales.dart';
import 'package:agrin/l10n/app_localizations.dart';
import 'package:agrin/screens/consent_screen.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:provider/provider.dart';
import 'package:shared_preferences/shared_preferences.dart';

http.Response _json(Object body, [int status = 200]) => http.Response(jsonEncode(body), status,
    headers: {'content-type': 'application/json; charset=utf-8'});

const _notice = {
  'version': 'v1',
  'title': 'How Agro Nova uses your data',
  'intro': 'You choose what to allow.',
  'purposes': [
    {'id': 'service', 'required': true, 'title': 'Your plots and farm details', 'description': 'Needed for advice.'},
    {'id': 'ai', 'required': false, 'title': 'AI advice and plant photos', 'description': 'Sent to AI services.'},
    {'id': 'notifications', 'required': false, 'title': 'Notifications', 'description': 'Answer ready.'},
  ],
  'sections': [
    {'title': 'How long we keep it', 'body': 'Until 730 days after you stop.'}
  ],
  'grievance_officer': {'name': 'A. Officer', 'email': 'dpo@example.org'},
};

void main() {
  late List<Map<String, dynamic>> puts;

  Future<void> show(WidgetTester tester, {bool manage = false, bool failNotice = false}) async {
    SharedPreferences.setMockInitialValues({});
    puts = [];
    tester.view.physicalSize = const Size(800, 3200); // tall, so the whole list is built
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);
    final api = Api(baseUrl: 'http://test', devUser: 't')..connectRetries = 0;
    final state = AppState(api);
    final client = MockClient((req) async {
      if (req.url.path.endsWith('/consent/notice')) {
        return failNotice ? throw http.ClientException('offline') : _json(_notice);
      }
      if (req.method == 'PUT') {
        final body = jsonDecode(req.body) as Map<String, dynamic>;
        puts.add(body);
        return _json({'needs_consent': false, 'purposes': body['purposes']});
      }
      return _json({});
    });
    await http.runWithClient(() async {
      await tester.pumpWidget(ChangeNotifierProvider.value(
        value: state,
        child: MaterialApp(
          localizationsDelegates: localizationDelegates,
          supportedLocales: AppLocalizations.supportedLocales,
          home: ConsentScreen(manage: manage),
        ),
      ));
      await tester.pumpAndSettle();
    }, () => client);
    // Later taps also need the fake server, so keep the client for the rest of the test.
    _client = client;
  }

  Future<void> tapAndSettle(WidgetTester tester, Finder f) => http.runWithClient(() async {
        await tester.ensureVisible(f);
        await tester.tap(f);
        await tester.pumpAndSettle();
      }, () => _client!);

  testWidgets('shows the notice with the required purpose locked on and nothing else pre-ticked', (tester) async {
    await show(tester);
    expect(find.text('How Agro Nova uses your data'), findsOneWidget);
    expect(find.text('Your plots and farm details'), findsOneWidget);
    expect(find.text('Required'), findsOneWidget);
    final switches = tester.widgetList<SwitchListTile>(find.byType(SwitchListTile)).toList();
    expect(switches.map((s) => s.value), [true, false, false], reason: 'consent must be a clear, unticked choice');
    expect(switches.first.onChanged, isNull, reason: 'the required purpose cannot be switched off');
    expect(find.textContaining('dpo@example.org'), findsOneWidget);
  });

  testWidgets('agreeing sends exactly what the farmer chose', (tester) async {
    await show(tester);
    await tapAndSettle(tester, find.text('AI advice and plant photos'));
    await tapAndSettle(tester, find.text('I agree and continue'));
    expect(puts.single['notice_version'], 'v1');
    expect(puts.single['purposes'], {'service': true, 'ai': true, 'notifications': false});
  });

  testWidgets('refusing shows why the app cannot be used and sends nothing', (tester) async {
    await show(tester);
    await tapAndSettle(tester, find.text('I do not agree'));
    expect(find.textContaining('cannot be used'), findsOneWidget);
    expect(puts, isEmpty);
  });

  testWidgets('with no connection the farmer can retry loading the notice', (tester) async {
    await show(tester, failNotice: true);
    expect(find.text('Try again'), findsOneWidget);
    expect(find.text('I agree and continue'), findsNothing, reason: 'nobody agrees to a notice they cannot read');
  });

  testWidgets('the privacy page offers download and delete next to the choices', (tester) async {
    await show(tester, manage: true);
    expect(find.text('Save my choices'), findsOneWidget);
    expect(find.text('Download my data'), findsOneWidget);
    expect(find.text('Delete my data'), findsOneWidget);
    expect(find.text('I do not agree'), findsNothing);
  });
}

http.Client? _client;
