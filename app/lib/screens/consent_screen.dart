import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../core/api.dart';
import '../core/app_state.dart';
import '../l10n/app_localizations.dart';
import 'privacy_actions.dart';

/// The privacy notice and the farmer's choices (India's DPDP Act: plain notice, a separate choice per purpose,
/// nothing pre-ticked, and withdrawing as easy as agreeing).
///
/// Shown before anything else on first use, and again when the notice changes. With [manage] it is the "Privacy and
/// my data" page opened from the menu, where the choices can be changed and the data downloaded or deleted.
/// The wording comes from the server (translated there), so a legal change needs no app release.
class ConsentScreen extends StatefulWidget {
  const ConsentScreen({super.key, this.manage = false});
  final bool manage;
  @override
  State<ConsentScreen> createState() => _ConsentScreenState();
}

class _ConsentScreenState extends State<ConsentScreen> {
  Map<String, dynamic>? _notice;
  final Map<String, bool> _choice = {};
  String? _error;
  bool _busy = false;
  bool _declined = false;

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    final state = context.read<AppState>();
    setState(() => _error = null);
    try {
      final n = await state.loadNotice();
      if (!mounted) return;
      setState(() {
        _notice = n;
        for (final p in n['purposes'] as List) {
          final id = p['id'] as String;
          // Nothing is pre-ticked for a first-time farmer; a returning one sees what they chose before.
          _choice[id] = p['required'] == true ? true : state.consented(id);
        }
      });
    } catch (e) {
      if (mounted) setState(() => _error = e.toString());
    }
  }

  Future<void> _submit() async {
    final t = AppLocalizations.of(context);
    final state = context.read<AppState>();
    final messenger = ScaffoldMessenger.of(context);
    final navigator = Navigator.of(context);
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      await state.acceptConsent(_notice!['version'] as String, _choice);
      if (widget.manage) {
        messenger.showSnackBar(SnackBar(content: Text(t.consentSaved)));
        navigator.pop();
      }
    } on ApiException catch (e) {
      if (e.status == 409) await _load(); // the notice changed while it was open: show the new one
      if (mounted) setState(() => _error = e.message);
    } catch (e) {
      if (mounted) setState(() => _error = e.toString());
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final t = AppLocalizations.of(context);
    final n = _notice;
    final theme = Theme.of(context);
    return Scaffold(
      appBar: AppBar(
        title: Text(widget.manage ? t.privacyMenu : t.consentTitle),
        automaticallyImplyLeading: widget.manage,
      ),
      body: n == null
          ? Center(
              child: _error == null
                  ? const CircularProgressIndicator()
                  : Padding(
                      padding: const EdgeInsets.all(24),
                      child: Column(mainAxisSize: MainAxisSize.min, children: [
                        Text(_error!, textAlign: TextAlign.center),
                        const SizedBox(height: 8),
                        Text(t.loadFailed, style: theme.textTheme.bodySmall),
                        const SizedBox(height: 12),
                        FilledButton(onPressed: _load, child: Text(t.retry)),
                      ]),
                    ),
            )
          : ListView(padding: const EdgeInsets.all(16), children: [
              Text(n['title'] ?? '', style: theme.textTheme.headlineSmall),
              const SizedBox(height: 8),
              Text(n['intro'] ?? ''),
              const SizedBox(height: 16),
              for (final p in n['purposes'] as List)
                Card(
                  child: SwitchListTile(
                    value: _choice[p['id']] ?? false,
                    // The required purpose is on and cannot be switched off: without it there is no service. A
                    // farmer who wants out deletes their data instead.
                    onChanged: p['required'] == true || _busy ? null : (v) => setState(() => _choice[p['id']] = v),
                    title: Text(p['title'] ?? ''),
                    subtitle: Padding(
                      padding: const EdgeInsets.only(top: 6),
                      child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                        if (p['required'] == true)
                          Text(t.consentRequired,
                              style: theme.textTheme.labelMedium?.copyWith(color: theme.colorScheme.primary)),
                        Text(p['description'] ?? ''),
                      ]),
                    ),
                    isThreeLine: true,
                  ),
                ),
              for (final s in n['sections'] as List)
                ExpansionTile(
                  title: Text(s['title'] ?? ''),
                  childrenPadding: const EdgeInsets.fromLTRB(16, 0, 16, 12),
                  expandedCrossAxisAlignment: CrossAxisAlignment.start,
                  children: [Text(s['body'] ?? '')],
                ),
              if (((n['grievance_officer'] as Map?)?['email'] ?? '').toString().isNotEmpty)
                Padding(
                  padding: const EdgeInsets.symmetric(vertical: 8),
                  child: Text(
                    '${t.consentContact}: ${n['grievance_officer']['name'] ?? ''} ${n['grievance_officer']['email']}',
                    style: theme.textTheme.bodySmall,
                  ),
                ),
              if (_error != null)
                Padding(
                  padding: const EdgeInsets.symmetric(vertical: 8),
                  child: Text(_error!, style: TextStyle(color: theme.colorScheme.error)),
                ),
              if (_declined && !widget.manage)
                Card(color: theme.colorScheme.errorContainer, child: Padding(padding: const EdgeInsets.all(16), child: Text(t.consentDeclinedBody))),
              const SizedBox(height: 8),
              FilledButton(
                onPressed: _busy ? null : _submit,
                child: _busy
                    ? const SizedBox(height: 20, width: 20, child: CircularProgressIndicator(strokeWidth: 2))
                    : Text(widget.manage ? t.consentSave : t.consentAgree),
              ),
              if (!widget.manage)
                TextButton(onPressed: _busy ? null : () => setState(() => _declined = true), child: Text(t.consentDecline)),
              if (widget.manage) ...[
                const Divider(height: 32),
                OutlinedButton.icon(
                    icon: const Icon(Icons.download_outlined), label: Text(t.downloadMyData), onPressed: () => showMyData(context)),
                const SizedBox(height: 8),
                OutlinedButton.icon(
                  icon: const Icon(Icons.delete_forever_outlined),
                  label: Text(t.deleteMyData),
                  style: OutlinedButton.styleFrom(foregroundColor: theme.colorScheme.error),
                  onPressed: () => confirmDeleteMyData(context),
                ),
              ],
              const SizedBox(height: 24),
            ]),
    );
  }
}
