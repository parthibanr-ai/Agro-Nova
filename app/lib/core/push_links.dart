import 'api.dart';

/// What a "your answer is ready" notification carries: which job finished, what kind of answer it is, and the plot
/// it was for (if any). The server puts these in the push's data (see the server's `jobs._notify`).
class PushLink {
  const PushLink({required this.kind, required this.jobId, this.plotId});

  final String kind;
  final String jobId;
  final String? plotId;

  /// Null for a push that is not an answer notification (a scheme or weather digest), which just opens the app.
  static PushLink? fromData(Map<String, dynamic> data) {
    final kind = data['kind'], job = data['job_id'], plot = data['plot_id'];
    if (kind is! String || job is! String || kind.isEmpty || job.isEmpty) return null;
    return PushLink(kind: kind, jobId: job, plotId: plot is String && plot.isNotEmpty ? plot : null);
  }
}

/// The finished job's result if the server still has it, else a fresh answer. A job's result is kept for about an
/// hour, so a notification tapped the next morning finds it gone: the screen then simply asks again (which is quick
/// when the answer is still cached on the server) instead of showing an error.
Future<dynamic> resultOrFresh(Api api, String? jobId, Future<dynamic> Function() fresh) async {
  if (jobId == null) return fresh();
  try {
    return await api.jobResult(jobId);
  } on ApiException catch (e) {
    if (e.status == 404) return fresh();
    rethrow;
  }
}
