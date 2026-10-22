class AppBackendConfig {
  static const String localBackendUrl = 'http://127.0.0.1:8001';
  static const String vpsBackendUrl = 'https://api.fkpharm.online';

  // Keep current local-first behavior unless explicitly disabled at build time.
  static const bool forceLocalBackend = bool.fromEnvironment(
    'FORCE_LOCAL_BACKEND',
    defaultValue: false,
  );

  static const String defaultBaseUrl =
      forceLocalBackend ? localBackendUrl : vpsBackendUrl;
}
