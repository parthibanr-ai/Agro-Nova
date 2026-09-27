import 'dart:async';

import 'package:flutter/material.dart';
import 'package:geolocator/geolocator.dart';
import 'package:google_maps_flutter/google_maps_flutter.dart';
import 'package:provider/provider.dart';

import '../core/api.dart';
import '../core/app_state.dart';
import '../l10n/app_localizations.dart';
import '../widgets.dart';

/// Capture the plot boundary: tap the map (or stand at each corner and press "Use my GPS").
/// The server re-validates geometry (self-intersection, area, inside the country).
class PlotCaptureScreen extends StatefulWidget {
  const PlotCaptureScreen({super.key});
  @override
  State<PlotCaptureScreen> createState() => _PlotCaptureScreenState();
}

class _PlotCaptureScreenState extends State<PlotCaptureScreen> {
  final _corners = <LatLng>[];
  final _name = TextEditingController();
  final _state = TextEditingController();
  String? _crop;
  String _country = 'IN';
  DateTime? _sowing;
  bool _saving = false;
  String? _error;
  GoogleMapController? _map;

  final _search = TextEditingController();
  Timer? _searchDebounce;
  List<dynamic> _searchResults = [];
  bool _searching = false;

  @override
  void dispose() {
    _searchDebounce?.cancel();
    _search.dispose();
    _name.dispose();
    _state.dispose();
    super.dispose();
  }

  void _onSearchChanged(String text) {
    _searchDebounce?.cancel();
    if (text.trim().isEmpty) {
      setState(() => _searchResults = []);
      return;
    }
    _searchDebounce = Timer(const Duration(milliseconds: 500), () async {
      setState(() => _searching = true);
      try {
        final api = context.read<AppState>().api;
        final res = await api.get('/geocode', query: {'q': text.trim()});
        if (mounted) setState(() => _searchResults = res['results'] as List<dynamic>);
      } catch (_) {
        if (mounted) setState(() => _searchResults = []);
      } finally {
        if (mounted) setState(() => _searching = false);
      }
    });
  }

  void _goToSearchResult(dynamic result) {
    final ll = LatLng((result['lat'] as num).toDouble(), (result['lon'] as num).toDouble());
    _map?.animateCamera(CameraUpdate.newLatLngZoom(ll, 16));
    setState(() => _searchResults = []);
    _search.clear();
    FocusScope.of(context).unfocus();
  }

  Future<void> _enterCoordinates() async {
    final t = AppLocalizations.of(context);
    final latController = TextEditingController();
    final lngController = TextEditingController();
    String? error;
    await showDialog<void>(
      context: context,
      builder: (dialogContext) => StatefulBuilder(
        builder: (dialogContext, setDialogState) => AlertDialog(
          title: Text(t.enterCoordinates),
          content: Column(mainAxisSize: MainAxisSize.min, children: [
            Padding(
              padding: const EdgeInsets.only(bottom: 12),
              child: Text(t.coordinateEntryHelp(_corners.length), style: Theme.of(dialogContext).textTheme.bodySmall),
            ),
            TextField(
              controller: latController,
              keyboardType: const TextInputType.numberWithOptions(decimal: true, signed: true),
              decoration: InputDecoration(labelText: t.latitude),
            ),
            const SizedBox(height: 12),
            TextField(
              controller: lngController,
              keyboardType: const TextInputType.numberWithOptions(decimal: true, signed: true),
              decoration: InputDecoration(labelText: t.longitude),
            ),
            if (error != null)
              Padding(
                padding: const EdgeInsets.only(top: 12),
                child: Text(error!, style: TextStyle(color: Theme.of(dialogContext).colorScheme.error)),
              ),
          ]),
          actions: [
            TextButton(onPressed: () => Navigator.of(dialogContext).pop(), child: Text(MaterialLocalizations.of(dialogContext).cancelButtonLabel)),
            FilledButton(
              onPressed: () {
                final lat = double.tryParse(latController.text.trim());
                final lng = double.tryParse(lngController.text.trim());
                if (lat == null || lng == null || lat < -90 || lat > 90 || lng < -180 || lng > 180) {
                  setDialogState(() => error = t.invalidCoordinates);
                  return;
                }
                final ll = LatLng(lat, lng);
                setState(() => _corners.add(ll));
                _map?.animateCamera(CameraUpdate.newLatLng(ll));
                Navigator.of(dialogContext).pop();
              },
              child: Text(t.addCorner),
            ),
          ],
        ),
      ),
    );
  }

  Future<void> _useGps() async {
    try {
      var perm = await Geolocator.checkPermission();
      if (perm == LocationPermission.denied) perm = await Geolocator.requestPermission();
      if (perm == LocationPermission.denied || perm == LocationPermission.deniedForever) {
        throw 'Location permission denied';
      }
      final p = await Geolocator.getCurrentPosition(locationSettings: const LocationSettings(accuracy: LocationAccuracy.best));
      final ll = LatLng(p.latitude, p.longitude);
      setState(() => _corners.add(ll));
      _map?.animateCamera(CameraUpdate.newLatLng(ll));
    } catch (e) {
      setState(() => _error = e.toString());
    }
  }

  Future<void> _save() async {
    if (_corners.length < 4 || _crop == null || _name.text.trim().isEmpty) return;
    setState(() {
      _saving = true;
      _error = null;
    });
    try {
      await context.read<AppState>().createPlot(
            name: _name.text.trim(),
            crop: _crop!,
            country: _country,
            state: _state.text.trim(),
            sowingDate: _sowing,
            corners: [for (final c in _corners) [c.latitude, c.longitude]],
          );
      if (mounted) Navigator.of(context).pop();
    } on ApiException catch (e) {
      setState(() => _error = e.message);
    } catch (e) {
      setState(() => _error = e.toString());
    } finally {
      if (mounted) setState(() => _saving = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final t = AppLocalizations.of(context);
    final api = context.read<AppState>().api;
    final ready = _corners.length >= 4 && _crop != null && _name.text.trim().isNotEmpty;

    return Scaffold(
      appBar: AppBar(title: Text(t.addPlot)),
      body: Column(children: [
        SizedBox(
          height: 300,
          child: Stack(children: [
            GoogleMap(
              mapType: MapType.hybrid, // satellite imagery with labels helps farmers recognise their field
              initialCameraPosition: const CameraPosition(target: LatLng(20.5937, 78.9629), zoom: 5),
              myLocationEnabled: true,
              onMapCreated: (c) => _map = c,
              onTap: (ll) => setState(() => _corners.add(ll)),
              markers: {
                for (var i = 0; i < _corners.length; i++)
                  Marker(markerId: MarkerId('c$i'), position: _corners[i], infoWindow: InfoWindow(title: '${i + 1}')),
              },
              polygons: {
                if (_corners.length >= 3)
                  Polygon(
                    polygonId: const PolygonId('plot'),
                    points: _corners,
                    strokeWidth: 2,
                    strokeColor: Colors.green.shade800,
                    fillColor: Colors.green.withValues(alpha: 0.25),
                  ),
              },
            ),
            Positioned(
              left: 8,
              right: 8,
              top: 8,
              child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
                Material(
                  elevation: 3,
                  borderRadius: BorderRadius.circular(8),
                  child: TextField(
                    controller: _search,
                    onChanged: _onSearchChanged,
                    decoration: InputDecoration(
                      hintText: t.searchLocation,
                      filled: true,
                      fillColor: Theme.of(context).colorScheme.surface,
                      prefixIcon: const Icon(Icons.search),
                      suffixIcon: _searching
                          ? const Padding(padding: EdgeInsets.all(12), child: SizedBox(width: 16, height: 16, child: CircularProgressIndicator(strokeWidth: 2)))
                          : null,
                      border: OutlineInputBorder(borderRadius: BorderRadius.circular(8), borderSide: BorderSide.none),
                      contentPadding: const EdgeInsets.symmetric(horizontal: 12),
                    ),
                  ),
                ),
                if (_search.text.isNotEmpty && !_searching)
                  Material(
                    elevation: 3,
                    borderRadius: BorderRadius.circular(8),
                    child: ConstrainedBox(
                      constraints: const BoxConstraints(maxHeight: 180),
                      child: _searchResults.isEmpty
                          ? Padding(padding: const EdgeInsets.all(12), child: Text(t.noResultsFound))
                          : ListView(
                              shrinkWrap: true,
                              children: [
                                for (final r in _searchResults)
                                  ListTile(
                                    dense: true,
                                    leading: const Icon(Icons.place_outlined),
                                    title: Text(r['display_name'] as String, maxLines: 2, overflow: TextOverflow.ellipsis),
                                    onTap: () => _goToSearchResult(r),
                                  ),
                              ],
                            ),
                    ),
                  ),
              ]),
            ),
          ]),
        ),
        Padding(
          padding: const EdgeInsets.fromLTRB(16, 8, 16, 0),
          child: Row(children: [
            Expanded(child: Text(_corners.isEmpty ? t.captureCorner : t.cornersCaptured(_corners.length))),
            IconButton(tooltip: t.useMyGps, icon: const Icon(Icons.my_location), onPressed: _useGps),
            IconButton(tooltip: t.enterCoordinates, icon: const Icon(Icons.edit_location_alt), onPressed: _enterCoordinates),
            IconButton(
              tooltip: t.undoCorner,
              icon: const Icon(Icons.undo),
              onPressed: _corners.isEmpty ? null : () => setState(_corners.removeLast),
            ),
          ]),
        ),
        Expanded(
          child: ListView(padding: const EdgeInsets.all(16), children: [
            TextField(controller: _name, decoration: InputDecoration(labelText: t.plotName, border: const OutlineInputBorder()), onChanged: (_) => setState(() {})),
            const SizedBox(height: 12),
            AsyncBody<dynamic>(
              load: () => api.get('/meta/crops'),
              builder: (_, data) => DropdownButtonFormField<String>(
                initialValue: _crop,
                decoration: InputDecoration(labelText: t.crop, border: const OutlineInputBorder()),
                items: [for (final c in data['crops']) DropdownMenuItem<String>(value: c['id'], child: Text(c['name']))],
                onChanged: (v) => setState(() => _crop = v),
              ),
            ),
            const SizedBox(height: 12),
            Row(children: [
              Expanded(
                child: DropdownButtonFormField<String>(
                  initialValue: _country,
                  decoration: const InputDecoration(labelText: 'Country', border: OutlineInputBorder()),
                  items: const [
                    DropdownMenuItem(value: 'IN', child: Text('India')),
                    DropdownMenuItem(value: 'BR', child: Text('Brasil')),
                    DropdownMenuItem(value: 'RU', child: Text('Россия')),
                    DropdownMenuItem(value: 'CN', child: Text('中国')),
                  ],
                  onChanged: (v) => setState(() => _country = v ?? 'IN'),
                ),
              ),
              const SizedBox(width: 12),
              Expanded(child: TextField(controller: _state, decoration: InputDecoration(labelText: t.state, border: const OutlineInputBorder()))),
            ]),
            const SizedBox(height: 12),
            OutlinedButton.icon(
              icon: const Icon(Icons.event),
              label: Text(_sowing == null ? t.sowingDate : _sowing!.toIso8601String().substring(0, 10)),
              onPressed: () async {
                final d = await showDatePicker(
                    context: context, initialDate: DateTime.now(), firstDate: DateTime(2020), lastDate: DateTime.now().add(const Duration(days: 365)));
                if (d != null) setState(() => _sowing = d);
              },
            ),
            if (_error != null)
              Padding(padding: const EdgeInsets.only(top: 12), child: Text(_error!, style: TextStyle(color: Theme.of(context).colorScheme.error))),
            const SizedBox(height: 16),
            FilledButton(onPressed: ready && !_saving ? _save : null, child: _saving ? const CircularProgressIndicator() : Text(t.savePlot)),
          ]),
        ),
      ]),
    );
  }
}
