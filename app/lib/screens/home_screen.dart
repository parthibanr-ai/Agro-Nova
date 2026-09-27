import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../core/app_state.dart';
import '../core/locales.dart';
import '../l10n/app_localizations.dart';
import 'crop_recommendation_screen.dart';
import 'diagnosis_screen.dart';
import 'forecast_screen.dart';
import 'info_screens.dart';
import 'plot_capture_screen.dart';
import 'soil_screen.dart';

class HomeScreen extends StatelessWidget {
  const HomeScreen({super.key});

  @override
  Widget build(BuildContext context) {
    final t = AppLocalizations.of(context);
    final s = context.watch<AppState>();
    final plot = s.selected;

    void open(Widget page) => Navigator.of(context).push(MaterialPageRoute(builder: (_) => page));

    final tiles = <(IconData, String, Widget Function())>[
      (Icons.cloud_outlined, t.forecast, () => ForecastScreen(plot: plot!)),
      (Icons.terrain, t.soil, () => SoilScreen(plot: plot!)),
      (Icons.eco_outlined, t.cropRecommendation, () => CropRecommendationScreen(plot: plot!)),
      (Icons.local_florist, t.diagnose, () => DiagnosisScreen(plot: plot)),
      (Icons.account_balance, t.schemes, () => SchemesScreen(plot: plot)),
      (Icons.pets, t.resilience, () => ResilienceScreen(plot: plot)),
      (Icons.water_drop_outlined, t.waterTips, () => WaterTipsScreen(plot: plot)),
      (Icons.storefront, t.market, () => MarketScreen(plot: plot!)),
    ];
    // Diagnosis, schemes, resilience and water tips work without a plot; the rest need one.
    const needsPlot = {0, 1, 2, 7};

    return Scaffold(
      appBar: AppBar(
        title: Text(t.appTitle),
        actions: [
          PopupMenuButton<String>(
            tooltip: t.language,
            icon: const Icon(Icons.translate),
            onSelected: (c) => context.read<AppState>().setLanguage(c),
            itemBuilder: (_) => [
              for (final l in supportedAgriLanguages)
                CheckedPopupMenuItem(value: l.code, checked: l.code == s.language, child: Text(l.native)),
            ],
          ),
        ],
      ),
      floatingActionButton: FloatingActionButton.extended(
        onPressed: () => open(const PlotCaptureScreen()),
        icon: const Icon(Icons.add_location_alt),
        label: Text(t.addPlot),
      ),
      body: s.loading
          ? const Center(child: CircularProgressIndicator())
          : ListView(padding: const EdgeInsets.all(16), children: [
              Text(t.tagline, style: Theme.of(context).textTheme.titleMedium),
              const SizedBox(height: 12),
              if (s.error != null) Text(s.error!, style: TextStyle(color: Theme.of(context).colorScheme.error)),
              if (s.plots.isEmpty)
                Card(child: Padding(padding: const EdgeInsets.all(16), child: Text(t.noPlots)))
              else
                DropdownButtonFormField<Plot>(
                  initialValue: plot,
                  isExpanded: true,
                  decoration: InputDecoration(labelText: t.myPlots, border: const OutlineInputBorder()),
                  items: [
                    for (final p in s.plots)
                      DropdownMenuItem(
                        value: p,
                        child: Text('${p.name} - ${p.crop} (${p.areaAcres} ac)', overflow: TextOverflow.ellipsis),
                      ),
                  ],
                  onChanged: (p) => p == null ? null : context.read<AppState>().select(p),
                ),
              const SizedBox(height: 16),
              for (var i = 0; i < tiles.length; i++)
                Card(
                  child: ListTile(
                    leading: Icon(tiles[i].$1),
                    title: Text(tiles[i].$2),
                    trailing: const Icon(Icons.chevron_right),
                    enabled: !needsPlot.contains(i) || plot != null,
                    onTap: () => open(tiles[i].$3()),
                  ),
                ),
              const SizedBox(height: 72),
            ]),
    );
  }
}
