class AppThresholds {
  static const double minNormalized = 0.0;
  static const double maxNormalized = 1.0;

  static const double defaultConfirmThreshold = 0.85;
  static const double fixedVerifyThreshold = 0.40;
  static const double defaultVerifyThreshold = fixedVerifyThreshold;
  static const double minExtractionThreshold = 0.20;

  static const double fixedCharacteristicsThreshold = 0.60;
  static const double lowReliabilityOnlyThreshold = 0.85;

  static double clampNormalized(double value) {
    if (value < minNormalized) return minNormalized;
    if (value > maxNormalized) return maxNormalized;
    return value;
  }

  static double clampExtractionThreshold(double value) {
    final clamped = clampNormalized(value);
    return clamped < minExtractionThreshold ? minExtractionThreshold : clamped;
  }
}
