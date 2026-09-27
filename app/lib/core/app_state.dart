import 'package:flutter/foundation.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'api.dart';

class Plot {
  Plot.fromJson(Map<String, dynamic> j)
      : id = j['id'],
        name = j['name'],
        crop = j['crop'],
        country = j['country'],
        areaAcres = (j['area_acres'] as num).toDouble(),
        lat = (j['centroid']['lat'] as num).toDouble(),
        lon = (j['centroid']['lon'] as num).toDouble(),
        corners = [for (final c in j['corners']) [(c['lat'] as num).toDouble(), (c['lon'] as num).toDouble()]];

  final String id, name, crop, country;
  final double areaAcres, lat, lon;
  final List<List<double>> corners;
}

class AppState extends ChangeNotifier {
  AppState(this.api);
  final Api api;

  String language = 'en';
  List<Plot> plots = [];
  Plot? selected;
  bool loading = false;
  String? error;

  Future<void> init() async {
    final prefs = await SharedPreferences.getInstance();
    language = prefs.getString('lang') ?? 'en';
    api.lang = language;
    await loadPlots();
  }

  Future<void> setLanguage(String code) async {
    language = code;
    api.lang = code;
    notifyListeners();
    try {
      (await SharedPreferences.getInstance()).setString('lang', code);
      await api.put('/me', {'language': code});
    } catch (_) {/* offline: preference is saved locally */}
  }

  Future<void> loadPlots() async {
    loading = true;
    error = null;
    notifyListeners();
    try {
      final data = await api.get('/plots');
      plots = [for (final p in data['plots']) Plot.fromJson(p)];
      selected = plots.isEmpty ? null : plots.firstWhere((p) => p.id == selected?.id, orElse: () => plots.first);
    } catch (e) {
      error = e.toString();
    }
    loading = false;
    notifyListeners();
  }

  void select(Plot p) {
    selected = p;
    notifyListeners();
  }

  /// Creates the plot; throws [ApiException] with the server's validation message on bad geometry.
  Future<Plot> createPlot({
    required String name,
    required String crop,
    required String country,
    String? state,
    DateTime? sowingDate,
    required List<List<double>> corners,
  }) async {
    final data = await api.post('/plots', {
      'name': name,
      'crop': crop,
      'country': country,
      if (state != null && state.isNotEmpty) 'state': state,
      if (sowingDate != null) 'sowing_date': sowingDate.toIso8601String().substring(0, 10),
      'corners': [for (final c in corners) {'lat': c[0], 'lon': c[1]}],
    });
    final plot = Plot.fromJson(data);
    plots = [...plots, plot];
    selected = plot;
    notifyListeners();
    return plot;
  }
}
