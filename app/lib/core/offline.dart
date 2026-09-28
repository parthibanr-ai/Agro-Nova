import 'dart:convert';

import 'package:shared_preferences/shared_preferences.dart';

/// An answer the server gave earlier, kept so the screen still opens with no signal.
class CachedAnswer {
  CachedAnswer(this.data, this.savedAt);
  final dynamic data;
  final DateTime savedAt;
}

/// The last answers the phone has seen, on the phone. Screens ask the server first and fall back to this only when
/// the server cannot be reached. Bounded (entry count and size), so it cannot grow without limit on a cheap phone.
class OfflineStore {
  OfflineStore(this._prefs);
  final SharedPreferences _prefs;

  static const maxEntries = 40;
  static const maxEntryBytes = 300 * 1024;
  static const _index = 'oc:index';
  static String _k(String key) => 'oc:$key';

  static Future<OfflineStore> open() async => OfflineStore(await SharedPreferences.getInstance());

  CachedAnswer? get(String key) {
    final raw = _prefs.getString(_k(key));
    if (raw == null) return null;
    try {
      final m = jsonDecode(raw) as Map<String, dynamic>;
      return CachedAnswer(m['d'], DateTime.parse(m['t'] as String));
    } catch (_) {
      return null; // a damaged entry is the same as no entry
    }
  }

  Future<void> put(String key, dynamic data) async {
    final raw = jsonEncode({'t': DateTime.now().toUtc().toIso8601String(), 'd': data});
    if (raw.length > maxEntryBytes) return;
    final keys = [..._prefs.getStringList(_index) ?? const <String>[]]..remove(key)..add(key);
    while (keys.length > maxEntries) {
      await _prefs.remove(_k(keys.removeAt(0))); // the least recently saved goes first
    }
    await _prefs.setString(_k(key), raw);
    await _prefs.setStringList(_index, keys);
  }

  Future<void> clear() async {
    for (final key in _prefs.getStringList(_index) ?? const <String>[]) {
      await _prefs.remove(_k(key));
    }
    await _prefs.remove(_index);
  }
}

/// A change made with no connection, waiting to be sent. [id] doubles as the `client_ref` the server uses to
/// recognise a retry, so sending one twice never creates two plots.
class QueuedWrite {
  QueuedWrite({
    required this.id,
    required this.kind,
    required this.method,
    required this.path,
    required this.body,
    this.plotRef,
    DateTime? createdAt,
  }) : createdAt = createdAt ?? DateTime.now().toUtc();

  factory QueuedWrite.fromJson(Map<String, dynamic> j) => QueuedWrite(
        id: j['id'],
        kind: j['kind'],
        method: j['method'],
        path: j['path'],
        body: Map<String, dynamic>.from(j['body'] as Map),
        plotRef: j['plotRef'],
        createdAt: DateTime.parse(j['createdAt']),
      );

  final String id;
  final String kind; // plot | soil | profile
  final String method;
  final String path; // may contain {plot}, filled in once the plot it belongs to has reached the server
  final Map<String, dynamic> body;
  final String? plotRef; // the client_ref of the plot this write belongs to, if that plot is itself still queued
  final DateTime createdAt;

  Map<String, dynamic> toJson() => {
        'id': id,
        'kind': kind,
        'method': method,
        'path': path,
        'body': body,
        'plotRef': plotRef,
        'createdAt': createdAt.toIso8601String(),
      };
}

/// Changes waiting for a connection, kept in order and across app restarts.
class WriteQueue {
  WriteQueue(this._prefs) {
    try {
      final raw = _prefs.getString(_key);
      if (raw != null) {
        _items.addAll([for (final j in jsonDecode(raw) as List) QueuedWrite.fromJson(Map<String, dynamic>.from(j))]);
      }
    } catch (_) {
      _items.clear();
    }
  }
  final SharedPreferences _prefs;
  final List<QueuedWrite> _items = [];
  static const _key = 'wq:items';

  static Future<WriteQueue> open() async => WriteQueue(await SharedPreferences.getInstance());

  List<QueuedWrite> get items => List.unmodifiable(_items);
  int get length => _items.length;

  Future<void> add(QueuedWrite w) async {
    if (w.kind == 'profile') _items.removeWhere((x) => x.kind == 'profile'); // only the latest profile matters
    _items.add(w);
    await _save();
  }

  Future<void> remove(String id) async {
    _items.removeWhere((w) => w.id == id);
    await _save();
  }

  Future<void> clear() async {
    _items.clear();
    await _save();
  }

  Future<void> _save() => _prefs.setString(_key, jsonEncode([for (final w in _items) w.toJson()]));
}
