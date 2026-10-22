import 'package:flutter/material.dart';

import '../api/client.dart';
import '../api/settings_api.dart';
import '../config/app_backend_config.dart';
import '../config/app_thresholds.dart';
import '../utils/file_opening.dart';

class SettingsScreen extends StatefulWidget {
  const SettingsScreen({super.key});

  @override
  State<SettingsScreen> createState() => _SettingsScreenState();
}

class _SettingsScreenState extends State<SettingsScreen> {
  bool _loading = true;
  String? _error;
  bool _gmailBusy = false;
  Map<String, dynamic>? _gmailAccount;

  final _hostController = TextEditingController();
  final _portController = TextEditingController();
  final _userController = TextEditingController();
  final _passController = TextEditingController();
  final _sendgridApiKeyController = TextEditingController();
  final _fromController = TextEditingController();
  final _replyController = TextEditingController();
  final _throttleController = TextEditingController();
  final _maxPerMinuteController = TextEditingController();
  final _maxPerDomainController = TextEditingController();
  final _retryMaxController = TextEditingController();
  final _retryDelayController = TextEditingController();
  final _embedConfirmController = TextEditingController();
  final _embedVerifyController = TextEditingController();
  final _embedExtractionController = TextEditingController();
  final _testEmailController = TextEditingController();
  final _apiUrlController = TextEditingController();

  bool _useTls = true;
  bool _modeMock = true;
  bool _useAiPromptForConversion = true;
  String _emailProvider = 'smtp';

  @override
  void initState() {
    super.initState();
    _apiUrlController.text = ApiClient.baseUrl;
    _load();
  }

  @override
  void dispose() {
    _hostController.dispose();
    _portController.dispose();
    _userController.dispose();
    _passController.dispose();
    _sendgridApiKeyController.dispose();
    _fromController.dispose();
    _replyController.dispose();
    _throttleController.dispose();
    _maxPerMinuteController.dispose();
    _maxPerDomainController.dispose();
    _retryMaxController.dispose();
    _retryDelayController.dispose();
    _embedConfirmController.dispose();
    _embedVerifyController.dispose();
    _embedExtractionController.dispose();
    _testEmailController.dispose();
    _apiUrlController.dispose();
    super.dispose();
  }

  Future<void> _load() async {
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      final settings = await SettingsApi.getSettings();
      final gmail = await SettingsApi.getGmailAccount();
      Map<String, dynamic>? thresholds;
      try {
        thresholds = await SettingsApi.getEmbeddingThresholds();
      } catch (_) {
        thresholds = null;
      }
      _emailProvider = (settings['email_provider'] ?? 'smtp').toString();
      _hostController.text = settings['smtp_host'] ?? '';
      _portController.text = (settings['smtp_port'] ?? 587).toString();
      _userController.text = settings['smtp_user'] ?? '';
      _passController.text = settings['smtp_password'] ?? '';
      _sendgridApiKeyController.text = settings['sendgrid_api_key'] ?? '';
      _fromController.text = settings['from_email'] ?? '';
      _replyController.text = settings['reply_to'] ?? '';
      _throttleController.text = settings['throttle_seconds'].toString();
      _maxPerMinuteController.text =
          settings['max_emails_per_minute'].toString();
      _maxPerDomainController.text =
          settings['max_emails_per_domain_per_minute'].toString();
      _retryMaxController.text = settings['retry_max_attempts'].toString();
      _retryDelayController.text =
          settings['retry_base_delay_seconds'].toString();
      _embedConfirmController.text = (thresholds?['confirm_threshold'] ??
              settings['embedding_confirm_threshold'] ??
              AppThresholds.defaultConfirmThreshold)
          .toString();
      _embedVerifyController.text =
          AppThresholds.fixedVerifyThreshold.toStringAsFixed(2);
      final extractionThreshold =
          (thresholds?['extraction_threshold'] as num?)?.toDouble() ??
              AppThresholds.minExtractionThreshold;
      _embedExtractionController.text =
          AppThresholds.clampExtractionThreshold(extractionThreshold)
              .toString();
      _useTls = settings['use_tls'] == true;
      _modeMock = settings['mode_mock'] == true;
      _useAiPromptForConversion =
          (settings['use_ai_prompt_for_conversion'] as bool?) ?? true;
      _gmailAccount = gmail;
    } catch (err) {
      _error = ApiClient.formatError(err);
    }
    if (mounted) {
      setState(() => _loading = false);
    }
  }

  Future<void> _save() async {
    final payload = {
      'email_provider': _emailProvider,
      'smtp_host': _hostController.text.trim(),
      'smtp_port': int.tryParse(_portController.text.trim()) ?? 587,
      'smtp_user': _userController.text.trim(),
      'smtp_password': _passController.text,
      'sendgrid_api_key': _sendgridApiKeyController.text.trim(),
      'from_email': _fromController.text.trim(),
      'reply_to': _replyController.text.trim(),
      'use_tls': _useTls,
      'mode_mock': _modeMock,
      'throttle_seconds':
          double.tryParse(_throttleController.text.trim()) ?? 1.0,
      'max_emails_per_minute':
          int.tryParse(_maxPerMinuteController.text.trim()) ?? 300,
      'max_emails_per_domain_per_minute':
          int.tryParse(_maxPerDomainController.text.trim()) ?? 60,
      'retry_max_attempts': int.tryParse(_retryMaxController.text.trim()) ?? 5,
      'retry_base_delay_seconds':
          int.tryParse(_retryDelayController.text.trim()) ?? 30,
      'embedding_confirm_threshold':
          double.tryParse(_embedConfirmController.text.trim()) ??
              AppThresholds.defaultConfirmThreshold,
      'embedding_verify_threshold': AppThresholds.fixedVerifyThreshold,
      'use_ai_prompt_for_conversion': _useAiPromptForConversion,
    };
    try {
      await SettingsApi.updateSettings(payload);
      await SettingsApi.updateEmbeddingThresholds({
        'confirm_threshold':
            double.tryParse(_embedConfirmController.text.trim()) ??
                AppThresholds.defaultConfirmThreshold,
        'verify_threshold': AppThresholds.fixedVerifyThreshold,
        'extraction_threshold': (() {
          final raw = double.tryParse(_embedExtractionController.text.trim()) ??
              AppThresholds.minExtractionThreshold;
          return AppThresholds.clampExtractionThreshold(raw);
        })(),
      });
      if (!mounted) return;
      ScaffoldMessenger.of(context)
          .showSnackBar(const SnackBar(content: Text('Settings saved')));
    } catch (err) {
      if (!mounted) return;
      ScaffoldMessenger.of(context)
          .showSnackBar(SnackBar(content: Text(ApiClient.formatError(err))));
    }
  }

  Future<void> _testEmail() async {
    final toEmail = _testEmailController.text.trim();
    if (toEmail.isEmpty) return;
    try {
      await SettingsApi.testEmail(toEmail);
      if (!mounted) return;
      ScaffoldMessenger.of(context)
          .showSnackBar(const SnackBar(content: Text('Test email sent')));
    } catch (err) {
      if (!mounted) return;
      ScaffoldMessenger.of(context)
          .showSnackBar(SnackBar(content: Text(ApiClient.formatError(err))));
    }
  }

  Future<void> _saveApiUrl() async {
    try {
      await ApiClient.setBaseUrl(_apiUrlController.text);
      _apiUrlController.text = ApiClient.baseUrl;
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text('Backend URL: ${ApiClient.baseUrl}')));
    } catch (err) {
      if (!mounted) return;
      ScaffoldMessenger.of(context)
          .showSnackBar(SnackBar(content: Text(ApiClient.formatError(err))));
    }
  }

  Future<void> _useLocalBackend() async {
    _apiUrlController.text = AppBackendConfig.localBackendUrl;
    await _saveApiUrl();
  }

  Future<void> _useVpsBackend() async {
    _apiUrlController.text = AppBackendConfig.vpsBackendUrl;
    await _saveApiUrl();
  }

  Future<bool> _openWithShell(String value) async {
    return openWithShell(value);
  }

  Future<void> _refreshGmailAccount() async {
    try {
      final gmail = await SettingsApi.getGmailAccount();
      if (!mounted) return;
      setState(() => _gmailAccount = gmail);
    } catch (_) {
      // Keep the current UI state if refresh fails.
    }
  }

  Future<void> _connectGmail() async {
    setState(() => _gmailBusy = true);
    try {
      final data = await SettingsApi.getGmailConnectUrl();
      var authUrl = (data['auth_url'] ?? '').toString();
      // Safety patch: keep OAuth callback on local backend port 8001.
      authUrl = authUrl.replaceAll(
        'redirect_uri=http%3A%2F%2F127.0.0.1%3A8000%2Fgmail%2Foauth%2Fcallback',
        'redirect_uri=http%3A%2F%2F127.0.0.1%3A8001%2Fgmail%2Foauth%2Fcallback',
      );
      authUrl = authUrl.replaceAll(
        'redirect_uri=http%3A%2F%2Flocalhost%3A8000%2Fgmail%2Foauth%2Fcallback',
        'redirect_uri=http%3A%2F%2Flocalhost%3A8001%2Fgmail%2Foauth%2Fcallback',
      );
      if (authUrl.isEmpty) {
        throw Exception('URL OAuth manquante');
      }
      final opened = await _openWithShell(authUrl);
      if (!mounted) return;
      if (!opened) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text('Ouvre ce lien manuellement: $authUrl')),
        );
      } else {
        ScaffoldMessenger.of(context).showSnackBar(
          const SnackBar(
            content: Text(
                'OAuth Gmail ouvert. Termine la connexion dans le navigateur.'),
          ),
        );
      }
    } catch (err) {
      if (!mounted) return;
      ScaffoldMessenger.of(context)
          .showSnackBar(SnackBar(content: Text(ApiClient.formatError(err))));
    } finally {
      if (mounted) setState(() => _gmailBusy = false);
    }
  }

  Future<void> _disconnectGmail() async {
    setState(() => _gmailBusy = true);
    try {
      await SettingsApi.disconnectGmail();
      await _refreshGmailAccount();
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(content: Text('Compte Gmail deconnecte')),
      );
    } catch (err) {
      if (!mounted) return;
      ScaffoldMessenger.of(context)
          .showSnackBar(SnackBar(content: Text(ApiClient.formatError(err))));
    } finally {
      if (mounted) setState(() => _gmailBusy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    if (_loading) {
      return const Center(child: CircularProgressIndicator());
    }
    if (_error != null) {
      return Center(child: Text('Error: $_error'));
    }
    return Padding(
      padding: const EdgeInsets.all(24),
      child: SingleChildScrollView(
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            const Text('Settings',
                style: TextStyle(fontSize: 22, fontWeight: FontWeight.bold)),
            const SizedBox(height: 16),
            const Text('Backend',
                style: TextStyle(fontWeight: FontWeight.bold)),
            const SizedBox(height: 8),
            Wrap(
              spacing: 8,
              runSpacing: 8,
              children: [
                OutlinedButton(
                  onPressed: _useLocalBackend,
                  child: const Text('Use LOCAL (127.0.0.1:8001)'),
                ),
                OutlinedButton(
                  onPressed: _useVpsBackend,
                  child: const Text('Use VPS (api.fkpharm.online)'),
                ),
              ],
            ),
            const SizedBox(height: 8),
            TextField(
              controller: _apiUrlController,
              decoration: const InputDecoration(labelText: 'Backend URL'),
            ),
            const SizedBox(height: 8),
            ElevatedButton(
                onPressed: _saveApiUrl, child: const Text('Save backend URL')),
            const SizedBox(height: 20),
            const Divider(),
            const SizedBox(height: 8),
            const Text('Gmail Import',
                style: TextStyle(fontWeight: FontWeight.bold)),
            const SizedBox(height: 6),
            Text(
              _gmailAccount == null
                  ? 'No Gmail account connected for this user.'
                  : 'Connected account: ${(_gmailAccount!['google_email'] ?? '').toString()}',
            ),
            if (_gmailAccount != null &&
                ((_gmailAccount!['last_error'] ?? '')
                    .toString()
                    .trim()
                    .isNotEmpty))
              Text(
                'Last sync error: ${(_gmailAccount!['last_error'] ?? '').toString()}',
                style: const TextStyle(color: Colors.redAccent),
              ),
            const SizedBox(height: 8),
            Wrap(
              spacing: 8,
              runSpacing: 8,
              children: [
                ElevatedButton(
                  onPressed: _gmailBusy ? null : _connectGmail,
                  child: const Text('Connect Gmail'),
                ),
                OutlinedButton(
                  onPressed: _gmailBusy ? null : _disconnectGmail,
                  child: const Text('Disconnect Gmail'),
                ),
                OutlinedButton(
                  onPressed: _gmailBusy ? null : _refreshGmailAccount,
                  child: const Text('Refresh Gmail status'),
                ),
              ],
            ),
            const SizedBox(height: 20),
            const Divider(),
            const SizedBox(height: 8),
            const Text('Email', style: TextStyle(fontWeight: FontWeight.bold)),
            DropdownButtonFormField<String>(
              value: _emailProvider,
              decoration: const InputDecoration(labelText: 'Email Provider'),
              items: const [
                DropdownMenuItem(value: 'smtp', child: Text('SMTP')),
                DropdownMenuItem(value: 'sendgrid', child: Text('SendGrid')),
              ],
              onChanged: (value) {
                if (value == null) return;
                setState(() => _emailProvider = value);
              },
            ),
            if (_emailProvider == 'smtp') ...[
              TextField(
                  controller: _hostController,
                  decoration: const InputDecoration(labelText: 'SMTP Host')),
              TextField(
                  controller: _portController,
                  decoration: const InputDecoration(labelText: 'SMTP Port')),
              TextField(
                  controller: _userController,
                  decoration: const InputDecoration(labelText: 'SMTP User')),
              TextField(
                controller: _passController,
                decoration: const InputDecoration(labelText: 'SMTP Password'),
                obscureText: true,
              ),
            ],
            if (_emailProvider == 'sendgrid')
              TextField(
                controller: _sendgridApiKeyController,
                decoration:
                    const InputDecoration(labelText: 'SendGrid API Key'),
                obscureText: true,
              ),
            TextField(
                controller: _fromController,
                decoration: const InputDecoration(labelText: 'From Email')),
            TextField(
                controller: _replyController,
                decoration: const InputDecoration(labelText: 'Reply-To')),
            TextField(
                controller: _throttleController,
                decoration:
                    const InputDecoration(labelText: 'Throttle Seconds')),
            TextField(
              controller: _maxPerMinuteController,
              decoration:
                  const InputDecoration(labelText: 'Max Emails Per Minute'),
            ),
            TextField(
              controller: _maxPerDomainController,
              decoration: const InputDecoration(
                  labelText: 'Max Emails Per Domain Per Minute'),
            ),
            TextField(
              controller: _retryMaxController,
              decoration:
                  const InputDecoration(labelText: 'Retry Max Attempts'),
            ),
            TextField(
              controller: _retryDelayController,
              decoration:
                  const InputDecoration(labelText: 'Retry Base Delay Seconds'),
            ),
            if (_emailProvider == 'smtp')
              SwitchListTile(
                value: _useTls,
                onChanged: (value) => setState(() => _useTls = value),
                title: const Text('Use TLS'),
              ),
            SwitchListTile(
              value: _modeMock,
              onChanged: (value) => setState(() => _modeMock = value),
              title: const Text('Mock mode (outbox)'),
            ),
            SwitchListTile(
              value: _useAiPromptForConversion,
              onChanged: (value) =>
                  setState(() => _useAiPromptForConversion = value),
              title: const Text(
                  'Utiliser Prompt IA pour decider de la conversion ?'),
            ),
            const SizedBox(height: 12),
            ElevatedButton(onPressed: _save, child: const Text('Save')),
            const SizedBox(height: 20),
            const Divider(),
            const SizedBox(height: 8),
            const Text('Matching',
                style: TextStyle(fontWeight: FontWeight.bold)),
            TextField(
              controller: _embedConfirmController,
              decoration: const InputDecoration(
                  labelText: 'Embedding Confirm Threshold (0-1)'),
            ),
            TextField(
              controller: _embedVerifyController,
              readOnly: true,
              enabled: false,
              decoration: const InputDecoration(
                labelText: 'Embedding Verify Threshold fixe (0.40)',
              ),
            ),
            TextField(
              controller: _embedExtractionController,
              decoration: const InputDecoration(
                labelText: 'Extraction Embedding Threshold (0.20-1)',
              ),
            ),
            const SizedBox(height: 12),
            const Divider(),
            const SizedBox(height: 8),
            const Text('Test Email',
                style: TextStyle(fontWeight: FontWeight.bold)),
            TextField(
                controller: _testEmailController,
                decoration: const InputDecoration(labelText: 'To Email')),
            const SizedBox(height: 8),
            OutlinedButton(
                onPressed: _testEmail, child: const Text('Send test email')),
          ],
        ),
      ),
    );
  }
}
