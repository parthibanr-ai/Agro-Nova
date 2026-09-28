import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:provider/provider.dart';

import '../core/app_state.dart';
import '../l10n/app_localizations.dart';

/// Asks first, then erases the farmer's data on the server (the right to erasure).
Future<void> confirmDeleteMyData(BuildContext context) async {
  final t = AppLocalizations.of(context);
  final state = context.read<AppState>();
  final messenger = ScaffoldMessenger.of(context);
  final ok = await showDialog<bool>(
    context: context,
    builder: (ctx) => AlertDialog(
      title: Text(t.deleteMyData),
      content: Text(t.deleteMyDataBody),
      actions: [
        TextButton(onPressed: () => Navigator.pop(ctx, false), child: Text(t.cancel)),
        FilledButton(
          style: FilledButton.styleFrom(backgroundColor: Theme.of(ctx).colorScheme.error),
          onPressed: () => Navigator.pop(ctx, true),
          child: Text(t.deleteConfirm),
        ),
      ],
    ),
  );
  if (ok != true) return;
  try {
    await state.deleteAccount();
    messenger.showSnackBar(SnackBar(content: Text(t.dataDeleted)));
  } catch (e) {
    messenger.showSnackBar(SnackBar(content: Text(e.toString())));
  }
}

/// Shows everything the server holds about the farmer (the right to access), with a button to copy it.
Future<void> showMyData(BuildContext context) async {
  final t = AppLocalizations.of(context);
  final api = context.read<AppState>().api;
  final messenger = ScaffoldMessenger.of(context);
  String text;
  try {
    text = const JsonEncoder.withIndent('  ').convert(await api.get('/me/export'));
  } catch (e) {
    messenger.showSnackBar(SnackBar(content: Text(e.toString())));
    return;
  }
  if (!context.mounted) return;
  await showDialog<void>(
    context: context,
    builder: (ctx) => AlertDialog(
      title: Text(t.downloadMyData),
      content: SizedBox(width: double.maxFinite, child: SingleChildScrollView(child: SelectableText(text))),
      actions: [
        TextButton(
          onPressed: () async {
            await Clipboard.setData(ClipboardData(text: text));
            messenger.showSnackBar(SnackBar(content: Text(t.copied)));
          },
          child: Text(t.copy),
        ),
        FilledButton(onPressed: () => Navigator.pop(ctx), child: Text(t.close)),
      ],
    ),
  );
}
