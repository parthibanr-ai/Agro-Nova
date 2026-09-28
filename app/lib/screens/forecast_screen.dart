import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../core/app_state.dart';
import '../core/push_links.dart';
import '../l10n/app_localizations.dart';
import '../widgets.dart';

class ForecastScreen extends StatelessWidget {
  const ForecastScreen({super.key, required this.plot, this.openJobId});
  final Plot plot;
  final String? openJobId;

  @override
  Widget build(BuildContext context) {
    final t = AppLocalizations.of(context);
    final api = context.read<AppState>().api;
    return Scaffold(
      appBar: AppBar(title: Text(t.forecast)),
      body: AsyncBody<dynamic>(
        load: () => resultOrFresh(api, openJobId, () => api.forecast(plot.id)),
        builder: (context, d) {
          final enso = d['enso'];
          final obs = d['observed'];
          final stage = d['growth_stage'];
          final daily = d['daily'] as List;
          return ListView(padding: const EdgeInsets.all(16), children: [
            if (stage != null)
              InfoCard(title: '${plot.crop}: ${stage['name']}', body: '${stage['days_after_sowing']} days after sowing'),
            if (enso['available'] == true)
              InfoCard(
                title: t.enso,
                body: '${enso['phase']} (${enso['strength']}, ONI ${enso['oni']}), ${enso['season']}',
              ),
            Text(t.advisories, style: Theme.of(context).textTheme.titleMedium),
            const SizedBox(height: 8),
            for (final a in d['advisories'])
              InfoCard(title: a['title'], body: a['detail'], severity: a['severity'], children: bullets(a['actions'])),
            if (obs != null)
              InfoCard(
                title: 'Last ${obs['window_days']} days',
                children: bullets([
                  if (obs['rain_mm'] != null) 'Rain ${obs['rain_mm']} mm (normal ${obs['rain_normal_mm']} mm)',
                  if (obs['tmean_c'] != null) 'Mean temperature ${obs['tmean_c']} °C',
                  if (obs['ndvi'] != null) 'NDVI ${obs['ndvi']} (last year ${obs['ndvi_normal']})',
                  if (obs['soil_moisture_pct'] != null) 'Soil moisture ${obs['soil_moisture_pct']}%',
                  'Sources: ${(obs['sources'] as List).join(', ')}',
                ]),
              ),
            InfoCard(title: '${daily.length}-day forecast', children: [
              for (final day in daily)
                Padding(
                  padding: const EdgeInsets.only(top: 4),
                  child: Row(children: [
                    SizedBox(width: 90, child: Text(day['date'].toString().substring(5))),
                    Expanded(child: Text('${day['tmin_c']}° / ${day['tmax_c']}°')),
                    Text('${day['rain_mm']} mm'),
                  ]),
                ),
            ]),
            Text(d['disclaimer'], style: Theme.of(context).textTheme.bodySmall),
          ]);
        },
      ),
    );
  }
}
