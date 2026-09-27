import 'package:flutter/material.dart';
import 'package:geolocator/geolocator.dart';
import 'package:provider/provider.dart';
import 'package:url_launcher/url_launcher.dart';

import '../core/app_state.dart';
import '../l10n/app_localizations.dart';
import '../widgets.dart';

class SoilScreen extends StatefulWidget {
  const SoilScreen({super.key, required this.plot});
  final Plot plot;
  @override
  State<SoilScreen> createState() => _SoilScreenState();
}

class _SoilScreenState extends State<SoilScreen> {
  int _reload = 0;

  @override
  Widget build(BuildContext context) {
    final t = AppLocalizations.of(context);
    final api = context.read<AppState>().api;
    return Scaffold(
      appBar: AppBar(title: Text(t.soil)),
      floatingActionButton: FloatingActionButton.extended(
        icon: const Icon(Icons.add),
        label: Text(t.addSoilSample),
        onPressed: () async {
          final saved = await Navigator.of(context).push<bool>(MaterialPageRoute(builder: (_) => SoilSampleForm(plot: widget.plot)));
          if (saved == true) setState(() => _reload++);
        },
      ),
      body: AsyncBody<dynamic>(
        key: ValueKey(_reload),
        load: () => api.get('/plots/${widget.plot.id}/soil'),
        builder: (context, d) {
          final prompt = d['prompt'];
          final pub = d['public_data'] as Map<String, dynamic>?;
          final samples = d['farmer_samples'] as List;
          return ListView(padding: const EdgeInsets.all(16), children: [
            InfoCard(title: t.soilAddData, body: prompt['message'], children: [
              if (prompt['enter_existing_data'] == true)
                Align(
                  alignment: Alignment.centerLeft,
                  child: TextButton.icon(
                    icon: const Icon(Icons.edit_note),
                    label: Text(t.addSoilSample),
                    onPressed: () async {
                      final saved = await Navigator.of(context).push<bool>(MaterialPageRoute(builder: (_) => SoilSampleForm(plot: widget.plot)));
                      if (saved == true) setState(() => _reload++);
                    },
                  ),
                ),
            ]),
            InfoCard(title: t.soilHowTo, children: [
              ...bullets(prompt['how_to_get_soil_tested']),
              for (final src in prompt['official_sources'])
                TextButton(onPressed: () => launchUrl(Uri.parse(src['url'])), child: Text(src['name'])),
            ]),
            if ((d['observations'] as List).isNotEmpty) InfoCard(title: t.soil, children: bullets(d['observations'])),
            for (final s in samples)
              InfoCard(
                title: '${s['source']}  (${(s['lat'] as num).toStringAsFixed(5)}, ${(s['lon'] as num).toStringAsFixed(5)})',
                children: bullets((s['values'] as Map).entries.map((e) => '${e.key}: ${e.value}')),
              ),
            if (pub != null)
              InfoCard(
                title: d['public_data_source'] ?? 'Public data',
                children: bullets(pub.values.map((v) => '${v['label']}: ${v['value']} ${v['unit']}')),
              ),
            const SizedBox(height: 72),
          ]);
        },
      ),
    );
  }
}

/// Manual soil entry. Captures the latitude/longitude of the sampling spot (GPS or typed).
class SoilSampleForm extends StatefulWidget {
  const SoilSampleForm({super.key, required this.plot});
  final Plot plot;
  @override
  State<SoilSampleForm> createState() => _SoilSampleFormState();
}

class _SoilSampleFormState extends State<SoilSampleForm> {
  late final _lat = TextEditingController(text: widget.plot.lat.toStringAsFixed(6));
  late final _lon = TextEditingController(text: widget.plot.lon.toStringAsFixed(6));
  final _values = {for (final k in ['ph', 'oc', 'n', 'p', 'k']) k: TextEditingController()};
  String _source = 'soil_health_card';
  String? _error;

  Future<void> _gps() async {
    try {
      var perm = await Geolocator.checkPermission();
      if (perm == LocationPermission.denied) perm = await Geolocator.requestPermission();
      final p = await Geolocator.getCurrentPosition(locationSettings: const LocationSettings(accuracy: LocationAccuracy.best));
      setState(() {
        _lat.text = p.latitude.toStringAsFixed(6);
        _lon.text = p.longitude.toStringAsFixed(6);
      });
    } catch (e) {
      setState(() => _error = e.toString());
    }
  }

  Future<void> _save() async {
    final vals = <String, double>{};
    _values.forEach((k, c) {
      final v = double.tryParse(c.text.trim());
      if (v != null) vals[k] = v;
    });
    try {
      await context.read<AppState>().api.post('/plots/${widget.plot.id}/soil', {
        'lat': double.parse(_lat.text),
        'lon': double.parse(_lon.text),
        'source': _source,
        'values': vals,
      });
      if (mounted) Navigator.of(context).pop(true);
    } catch (e) {
      setState(() => _error = e.toString());
    }
  }

  @override
  Widget build(BuildContext context) {
    final t = AppLocalizations.of(context);
    final labels = {'ph': t.ph, 'oc': t.organicCarbon, 'n': t.nitrogen, 'p': t.phosphorus, 'k': t.potassium};
    return Scaffold(
      appBar: AppBar(title: Text(t.addSoilSample)),
      body: ListView(padding: const EdgeInsets.all(16), children: [
        DropdownButtonFormField<String>(
          initialValue: _source,
          decoration: const InputDecoration(border: OutlineInputBorder()),
          items: const [
            DropdownMenuItem(value: 'soil_health_card', child: Text('Soil Health Card / govt report')),
            DropdownMenuItem(value: 'lab_test', child: Text('Private lab test')),
            DropdownMenuItem(value: 'field_kit', child: Text('Field test kit')),
          ],
          onChanged: (v) => setState(() => _source = v ?? _source),
        ),
        const SizedBox(height: 12),
        Text(t.sampleLocation, style: Theme.of(context).textTheme.titleSmall),
        Row(children: [
          Expanded(child: TextField(controller: _lat, keyboardType: const TextInputType.numberWithOptions(decimal: true, signed: true), decoration: const InputDecoration(labelText: 'Latitude'))),
          const SizedBox(width: 12),
          Expanded(child: TextField(controller: _lon, keyboardType: const TextInputType.numberWithOptions(decimal: true, signed: true), decoration: const InputDecoration(labelText: 'Longitude'))),
          IconButton(tooltip: t.useMyGps, icon: const Icon(Icons.my_location), onPressed: _gps),
        ]),
        const SizedBox(height: 12),
        for (final e in _values.entries)
          Padding(
            padding: const EdgeInsets.only(bottom: 12),
            child: TextField(
              controller: e.value,
              keyboardType: const TextInputType.numberWithOptions(decimal: true),
              decoration: InputDecoration(labelText: labels[e.key], border: const OutlineInputBorder()),
            ),
          ),
        if (_error != null) Text(_error!, style: TextStyle(color: Theme.of(context).colorScheme.error)),
        FilledButton(onPressed: _save, child: Text(t.save)),
      ]),
    );
  }
}
