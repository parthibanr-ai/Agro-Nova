import 'package:flutter/material.dart';
import 'package:provider/provider.dart';
import 'package:url_launcher/url_launcher.dart';

import '../core/app_state.dart';
import '../l10n/app_localizations.dart';
import '../widgets.dart';

class SchemesScreen extends StatelessWidget {
  const SchemesScreen({super.key, this.plot});
  final Plot? plot;

  @override
  Widget build(BuildContext context) {
    final t = AppLocalizations.of(context);
    final api = context.read<AppState>().api;
    return Scaffold(
      appBar: AppBar(title: Text(t.schemes)),
      body: AsyncBody<dynamic>(
        load: () => api.get('/schemes', query: {if (plot != null) 'plot_id': plot!.id}),
        builder: (_, d) => ListView(padding: const EdgeInsets.all(16), children: [
          for (final s in d['schemes'])
            InfoCard(title: s['name'], body: s['summary'], children: [
              const SizedBox(height: 6),
              Text(s['how_to_apply']),
              Align(
                alignment: Alignment.centerLeft,
                child: TextButton(onPressed: () => launchUrl(Uri.parse(s['url'])), child: Text(t.openOfficialSite)),
              ),
            ]),
          Text(d['note'], style: Theme.of(context).textTheme.bodySmall),
        ]),
      ),
    );
  }
}

class ResilienceScreen extends StatefulWidget {
  const ResilienceScreen({super.key, this.plot});
  final Plot? plot;
  @override
  State<ResilienceScreen> createState() => _ResilienceScreenState();
}

class _ResilienceScreenState extends State<ResilienceScreen> {
  final _cows = TextEditingController(text: '2');
  Map<String, dynamic>? _estimate;
  String? _error;

  Future<void> _calc() async {
    try {
      final r = await context.read<AppState>().api.post('/resilience/livestock-estimate', {
        'cows': int.parse(_cows.text),
        if (widget.plot != null) 'area_acres': widget.plot!.areaAcres,
      });
      setState(() {
        _estimate = Map<String, dynamic>.from(r);
        _error = null;
      });
    } catch (e) {
      setState(() => _error = e.toString());
    }
  }

  @override
  Widget build(BuildContext context) {
    final t = AppLocalizations.of(context);
    final api = context.read<AppState>().api;
    final e = _estimate;
    return Scaffold(
      appBar: AppBar(title: Text(t.resilience)),
      body: AsyncBody<dynamic>(
        load: () => api.resilience(plotId: widget.plot?.id),
        builder: (_, d) => ListView(padding: const EdgeInsets.all(16), children: [
          ...personalizedAdviceCards(context, d['personalized']),
          if (d['personalized'] == null && widget.plot == null)
            Padding(padding: const EdgeInsets.only(bottom: 12), child: Text(t.selectPlotForAdvice, style: Theme.of(context).textTheme.bodySmall)),
          if (d['personalized'] != null) Text(t.generalInformation, style: Theme.of(context).textTheme.titleMedium),
          if (d['personalized'] != null) const SizedBox(height: 8),
          for (final s in d['steps']) InfoCard(title: '${s['order']}. ${s['title']}', body: s['detail']),
          if (d['suggested_cows_for_manure_self_sufficiency'] != null)
            InfoCard(title: t.numberOfCows, body: '~${d['suggested_cows_for_manure_self_sufficiency']} cows can supply the manure for your plot.'),
          Row(children: [
            Expanded(child: TextField(controller: _cows, keyboardType: TextInputType.number, decoration: InputDecoration(labelText: t.numberOfCows, border: const OutlineInputBorder()))),
            const SizedBox(width: 12),
            FilledButton(onPressed: _calc, child: Text(t.calculate)),
          ]),
          if (_error != null) Text(_error!, style: TextStyle(color: Theme.of(context).colorScheme.error)),
          if (e != null)
            InfoCard(title: t.calculate, children: [
              ...bullets([
                'Milk: ${e['milk_litres_per_year']} L/year -> income ${e['milk_income_per_year']}',
                'Feed cost: ${e['feed_cost_per_year']}/year; net milk margin ${e['net_milk_margin_per_year']}',
                'Dung: ${e['dung_tonnes_per_year']} t/year -> compost ${e['compost_tonnes_per_year']} t (${e['acres_manured_per_year']} acres)',
                'Biogas: ${e['biogas_m3_per_day']} m³/day',
                if (e['manure_self_sufficiency_pct'] != null) 'Manure self-sufficiency: ${e['manure_self_sufficiency_pct']}%',
              ]),
              ...bullets(e['notes']),
            ]),
          for (final en in d['enterprises']) InfoCard(title: en['name'], body: en['returns']),
        ]),
      ),
    );
  }
}

class WaterTipsScreen extends StatelessWidget {
  const WaterTipsScreen({super.key, this.plot});
  final Plot? plot;

  @override
  Widget build(BuildContext context) {
    final t = AppLocalizations.of(context);
    final api = context.read<AppState>().api;
    return Scaffold(
      appBar: AppBar(title: Text(t.waterTips)),
      body: AsyncBody<dynamic>(
        load: () => api.waterTips(plotId: plot?.id),
        builder: (_, d) => ListView(padding: const EdgeInsets.all(16), children: [
          ...personalizedAdviceCards(context, d['personalized']),
          if (d['personalized'] == null && plot == null)
            Padding(padding: const EdgeInsets.only(bottom: 12), child: Text(t.selectPlotForAdvice, style: Theme.of(context).textTheme.bodySmall)),
          if (d['personalized'] != null) Text(t.generalInformation, style: Theme.of(context).textTheme.titleMedium),
          if (d['personalized'] != null) const SizedBox(height: 8),
          for (final tip in d['tips']) InfoCard(title: tip['title'], body: tip['detail']),
        ]),
      ),
    );
  }
}

class MarketScreen extends StatelessWidget {
  const MarketScreen({super.key, required this.plot});
  final Plot plot;

  @override
  Widget build(BuildContext context) {
    final t = AppLocalizations.of(context);
    final api = context.read<AppState>().api;
    return Scaffold(
      appBar: AppBar(title: Text(t.market)),
      body: AsyncBody<dynamic>(
        load: () => api.market(plot.id),
        builder: (_, d) => ListView(padding: const EdgeInsets.all(16), children: [
          ...personalizedAdviceCards(context, d['personalized']),
          if (d['personalized'] != null) Text(t.generalInformation, style: Theme.of(context).textTheme.titleMedium),
          if (d['personalized'] != null) const SizedBox(height: 8),
          InfoCard(title: plot.crop, children: bullets(d['value_addition_ideas'])),
          InfoCard(title: t.market, children: [...bullets(d['market_channels']), ...bullets(d['principles'])]),
          if (d['livestock_value_addition'] != null) ...[
            InfoCard(title: 'Milk', children: bullets(d['livestock_value_addition']['milk'])),
            InfoCard(title: 'Dung / manure', children: bullets(d['livestock_value_addition']['dung'])),
          ],
          for (final s in d['support_schemes'])
            InfoCard(title: s['name'], body: s['summary'], children: [
              TextButton(onPressed: () => launchUrl(Uri.parse(s['url'])), child: Text(t.openOfficialSite)),
            ]),
        ]),
      ),
    );
  }
}
