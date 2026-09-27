import 'package:flutter/material.dart';

import 'l10n/app_localizations.dart';

/// Loads [future] and renders [builder] with loading / error+retry states.
class AsyncBody<T> extends StatefulWidget {
  const AsyncBody({super.key, required this.load, required this.builder});
  final Future<T> Function() load;
  final Widget Function(BuildContext, T) builder;

  @override
  State<AsyncBody<T>> createState() => _AsyncBodyState<T>();
}

class _AsyncBodyState<T> extends State<AsyncBody<T>> {
  late Future<T> _f = widget.load();

  @override
  Widget build(BuildContext context) {
    return FutureBuilder<T>(
      future: _f,
      builder: (context, snap) {
        if (snap.connectionState != ConnectionState.done) {
          return const Center(child: CircularProgressIndicator());
        }
        if (snap.hasError) {
          final t = AppLocalizations.of(context);
          return Center(
            child: Padding(
              padding: const EdgeInsets.all(24),
              child: Column(mainAxisSize: MainAxisSize.min, children: [
                Text(snap.error.toString(), textAlign: TextAlign.center),
                const SizedBox(height: 8),
                Text(t.loadFailed, style: Theme.of(context).textTheme.bodySmall),
                const SizedBox(height: 12),
                FilledButton(onPressed: () => setState(() { _f = widget.load(); }), child: Text(t.retry)),
              ]),
            ),
          );
        }
        return widget.builder(context, snap.data as T);
      },
    );
  }
}

Color severityColor(BuildContext c, String? s) {
  switch (s) {
    case 'high':
      return Colors.red.shade700;
    case 'medium':
      return Colors.orange.shade800;
    case 'low':
      return Colors.amber.shade800;
    default:
      return Theme.of(c).colorScheme.primary;
  }
}

/// A titled card with an optional severity accent.
class InfoCard extends StatelessWidget {
  const InfoCard({super.key, required this.title, this.body, this.severity, this.children = const []});
  final String title;
  final String? body;
  final String? severity;
  final List<Widget> children;

  @override
  Widget build(BuildContext context) {
    return Card(
      margin: const EdgeInsets.only(bottom: 12),
      clipBehavior: Clip.antiAlias,
      child: Container(
        decoration: BoxDecoration(border: Border(left: BorderSide(width: 5, color: severityColor(context, severity)))),
        padding: const EdgeInsets.all(14),
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Text(title, style: Theme.of(context).textTheme.titleMedium),
          if (body != null) ...[const SizedBox(height: 6), Text(body!)],
          ...children,
        ]),
      ),
    );
  }
}

List<Widget> bullets(Iterable<dynamic> items) => [for (final i in items) Padding(padding: const EdgeInsets.only(top: 4), child: Text('•  $i'))];

/// The hyper-personalised LLM advice block (`personalized` field: {summary, items: [{title, detail, why}]})
/// shared by the resilience, water-tips and market screens. Renders nothing if [personalized] is null,
/// e.g. no plot is selected yet or the advice service is unavailable.
List<Widget> personalizedAdviceCards(BuildContext context, dynamic personalized) {
  if (personalized == null) return const [];
  final t = AppLocalizations.of(context);
  final items = (personalized['items'] as List?) ?? const [];
  return [
    InfoCard(
      title: t.personalizedForYourPlot,
      body: personalized['summary'] as String?,
      children: [
        for (final i in items) ...[
          const SizedBox(height: 10),
          Text(i['title'] ?? '', style: Theme.of(context).textTheme.titleSmall?.copyWith(fontWeight: FontWeight.bold)),
          const SizedBox(height: 4),
          Text(i['detail'] ?? ''),
          if ((i['why'] as String?)?.isNotEmpty == true) ...[
            const SizedBox(height: 4),
            Text('${t.why}: ${i['why']}', style: Theme.of(context).textTheme.bodySmall?.copyWith(fontStyle: FontStyle.italic)),
          ],
        ],
      ],
    ),
  ];
}
