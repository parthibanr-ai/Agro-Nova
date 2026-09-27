import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../core/app_state.dart';
import '../l10n/app_localizations.dart';
import '../widgets.dart';

class CropRecommendationScreen extends StatelessWidget {
  const CropRecommendationScreen({super.key, required this.plot});
  final Plot plot;

  Color _suitabilityColor(BuildContext context, String? suitability) {
    switch (suitability) {
      case 'high':
        return Colors.green.shade700;
      case 'low':
        return Colors.grey.shade600;
      default:
        return Colors.amber.shade800;
    }
  }

  @override
  Widget build(BuildContext context) {
    final t = AppLocalizations.of(context);
    final api = context.read<AppState>().api;
    return Scaffold(
      appBar: AppBar(title: Text(t.cropRecommendation)),
      body: AsyncBody<dynamic>(
        load: () => api.get('/plots/${plot.id}/crop-recommendation'),
        builder: (context, d) {
          final season = d['target_season'];
          final recs = d['recommendations'] as List;
          return ListView(padding: const EdgeInsets.all(16), children: [
            if (season != null && (season['local_name'] as String?)?.isNotEmpty == true)
              InfoCard(title: t.nextSowingSeason, body: '${season['local_name']} - ${season['months'] ?? ''}'),
            for (final r in recs)
              InfoCard(
                title: r['crop_name'],
                children: [
                  Row(children: [
                    Container(
                      padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 4),
                      decoration: BoxDecoration(
                        color: _suitabilityColor(context, r['suitability']).withValues(alpha: 0.15),
                        borderRadius: BorderRadius.circular(20),
                      ),
                      child: Text(
                        '${t.suitability}: ${r['suitability']}',
                        style: TextStyle(color: _suitabilityColor(context, r['suitability']), fontWeight: FontWeight.w600),
                      ),
                    ),
                  ]),
                  const SizedBox(height: 8),
                  Text(r['reasoning'] ?? ''),
                  if ((r['water_and_soil_fit'] as String?)?.isNotEmpty == true) ...[
                    const SizedBox(height: 8),
                    Text(t.waterAndSoilFit, style: Theme.of(context).textTheme.labelLarge),
                    Text(r['water_and_soil_fit']),
                  ],
                  if ((r['risks'] as List).isNotEmpty) ...[
                    const SizedBox(height: 8),
                    Text(t.risks, style: Theme.of(context).textTheme.labelLarge),
                    ...bullets(r['risks']),
                  ],
                ],
              ),
            if ((d['basis_summary'] as String?)?.isNotEmpty == true)
              InfoCard(title: t.basisForSuggestion, body: d['basis_summary']),
            const SizedBox(height: 8),
            Text(d['disclaimer'] ?? '', style: Theme.of(context).textTheme.bodySmall),
          ]);
        },
      ),
    );
  }
}
