import 'package:flutter/widgets.dart';

import '../core/app_state.dart';
import '../core/push_links.dart';
import 'crop_recommendation_screen.dart';
import 'diagnosis_screen.dart';
import 'forecast_screen.dart';
import 'info_screens.dart';

/// The screen a tapped "your answer is ready" notification opens, showing that finished answer.
/// Null when the link cannot be shown (an unknown kind, or a plot that no longer exists), so the app just opens.
Widget? screenForPush(PushLink link, List<Plot> plots) {
  Plot? plot;
  for (final p in plots) {
    if (!p.pending && p.id == link.plotId) plot = p;
  }
  switch (link.kind) {
    case 'crop_recommendation':
      return plot == null ? null : CropRecommendationScreen(plot: plot, openJobId: link.jobId);
    case 'forecast':
      return plot == null ? null : ForecastScreen(plot: plot, openJobId: link.jobId);
    case 'market':
      return plot == null ? null : MarketScreen(plot: plot, openJobId: link.jobId);
    case 'diagnosis':
      return DiagnosisScreen(plot: plot, openJobId: link.jobId);
    case 'resilience':
      return ResilienceScreen(plot: plot, openJobId: link.jobId);
    case 'water_tips':
      return WaterTipsScreen(plot: plot, openJobId: link.jobId);
  }
  return null;
}
