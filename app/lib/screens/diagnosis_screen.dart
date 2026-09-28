import 'dart:typed_data';

import 'package:flutter/material.dart';
import 'package:image_picker/image_picker.dart';
import 'package:provider/provider.dart';

import '../core/app_state.dart';
import '../l10n/app_localizations.dart';
import '../widgets.dart';

/// Photo -> Gemini diagnosis -> organic remedy, with the organic-vs-chemical explanation.
class DiagnosisScreen extends StatefulWidget {
  const DiagnosisScreen({super.key, this.plot, this.openJobId});
  final Plot? plot;

  /// Set when opened from a "ready" notification: show that finished diagnosis.
  final String? openJobId;
  @override
  State<DiagnosisScreen> createState() => _DiagnosisScreenState();
}

class _DiagnosisScreenState extends State<DiagnosisScreen> {
  Uint8List? _bytes;
  Map<String, dynamic>? _result;
  bool _busy = false;
  String? _error;

  @override
  void initState() {
    super.initState();
    final id = widget.openJobId;
    if (id != null) {
      _busy = true;
      context.read<AppState>().api.jobResult(id).then((r) {
        if (mounted) setState(() => _result = Map<String, dynamic>.from(r));
      }).catchError((Object e) {
        if (mounted) setState(() => _error = e.toString());
      }).whenComplete(() {
        if (mounted) setState(() => _busy = false);
      });
    }
  }

  Future<void> _pick(ImageSource source) async {
    final file = await ImagePicker().pickImage(source: source, maxWidth: 1600, imageQuality: 85);
    if (file == null) return;
    final bytes = await file.readAsBytes();
    setState(() {
      _bytes = bytes;
      _result = null;
      _error = null;
      _busy = true;
    });
    try {
      final api = context.read<AppState>().api;
      final r = await api.diagnose(bytes, file.name, plotId: widget.plot?.id, crop: widget.plot?.crop);
      setState(() => _result = Map<String, dynamic>.from(r));
    } catch (e) {
      setState(() => _error = e.toString());
    } finally {
      setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final t = AppLocalizations.of(context);
    final r = _result;
    return Scaffold(
      appBar: AppBar(title: Text(t.diagnose)),
      body: ListView(padding: const EdgeInsets.all(16), children: [
        Row(children: [
          Expanded(child: FilledButton.icon(icon: const Icon(Icons.photo_camera), label: Text(t.takePhoto), onPressed: _busy ? null : () => _pick(ImageSource.camera))),
          const SizedBox(width: 12),
          Expanded(child: OutlinedButton.icon(icon: const Icon(Icons.photo_library), label: Text(t.fromGallery), onPressed: _busy ? null : () => _pick(ImageSource.gallery))),
        ]),
        if (_bytes != null) Padding(padding: const EdgeInsets.symmetric(vertical: 12), child: ClipRRect(borderRadius: BorderRadius.circular(12), child: Image.memory(_bytes!, height: 220, fit: BoxFit.cover))),
        if (_busy) Padding(padding: const EdgeInsets.all(16), child: Row(children: [const CircularProgressIndicator(), const SizedBox(width: 16), Text(t.analysing)])),
        if (_error != null) Text(_error!, style: TextStyle(color: Theme.of(context).colorScheme.error)),
        if (r != null) ...[
          InfoCard(
            title: r['condition_name'],
            severity: r['severity'],
            body: r['message'] ?? 'Confidence ${(r['confidence'] * 100).round()}%',
            children: [
              if (r['symptoms_observed'] != null) ...bullets(r['symptoms_observed']),
            ],
          ),
          if (r['organic_remedies'] != null) ...[
            Text(t.organicRemedy, style: Theme.of(context).textTheme.titleMedium),
            const SizedBox(height: 8),
            for (final rem in r['organic_remedies'])
              InfoCard(title: rem['name'], children: [
                const SizedBox(height: 6),
                Text('Prepare: ${rem['prepare']}'),
                Text('Apply: ${rem['apply']}'),
                Text('When: ${rem['timing']}'),
              ]),
            if (r['prevention'] != null) InfoCard(title: 'Prevention', children: bullets(r['prevention'])),
          ],
          if (r['why_organic'] != null)
            InfoCard(title: t.whyOrganic, body: r['why_organic']['vs_chemical'], children: bullets(r['why_organic']['advantages'])),
          Text(r['disclaimer'] ?? '', style: Theme.of(context).textTheme.bodySmall),
        ],
      ]),
    );
  }
}
