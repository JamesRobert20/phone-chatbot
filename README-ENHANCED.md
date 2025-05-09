# Enhanced Dial-in Bot

This README describes the enhanced dial-in bot implementation with additional features for better call handling.

## Features

The enhanced dial-in bot extends the basic dial-in bot with the following features:

1. **Silence Detection**: Automatically detects when users have been silent for too long (default: 10 seconds)
2. **Automatic Prompting**: Sends a TTS prompt after extended silence to check if the user is still there
3. **Graceful Call Termination**: Automatically ends the call after a configurable number of unanswered prompts (default: 3)
4. **Call Statistics**: Provides detailed statistics about the call including duration, silence events, and more

## Usage

### Testing in Daily Prebuilt (No Actual Phone Calls)

```shell
curl -X POST "http://localhost:7860/start" \
     -H "Content-Type: application/json" \
     -d '{
         "config": {
            "enhanced_dialin": {
               "testInPrebuilt": true,
               "silenceThresholdSeconds": 10,
               "maxUnansweredPrompts": 3
            }
         }
      }'
```

This returns a Daily room URL where you can test the bot's conversation capabilities with silence detection.

### Using with Real Phone Calls

For incoming calls from customers, Daily will send a webhook to your `/start` endpoint. The bot will automatically use the enhanced dial-in features if configured as the default in `bot_constants.py`.

## Configuration Options

The enhanced dial-in bot can be configured with the following options:

- `silenceThresholdSeconds`: Number of seconds of silence before triggering a prompt (default: 10)
- `maxUnansweredPrompts`: Maximum number of unanswered prompts before ending the call (default: 3)
- `testInPrebuilt`: Whether to test in Daily Prebuilt (default: false)

Example configuration:

```json
{
  "config": {
    "enhanced_dialin": {
      "silenceThresholdSeconds": 15,
      "maxUnansweredPrompts": 2,
      "testInPrebuilt": true
    }
  }
}
```

## Call Statistics

After each call, detailed statistics are logged automatically. These statistics include:

- Call start and end time
- Total duration in seconds
- Number of silence events detected
- Number of silence prompts sent
- Number of unanswered prompts
- Longest and total silence duration
- Reason for call termination

These statistics are available in the logs and can be extended to be saved to a database or sent to an analytics platform.

## Implementation Details

The enhanced dial-in bot includes several custom components:

1. **CallStats**: Tracks and reports call statistics
2. **SilenceMonitor**: Monitors the call for extended silence and triggers appropriate actions
3. **Event Handlers**: Manages call lifecycle events and logs statistics

The implementation is based on the Pipecat framework and uses the SileroVADAnalyzer for voice activity detection. 