#
# Copyright (c) 2024–2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#
import argparse
import asyncio
import os
import sys
import time
from datetime import datetime

from call_connection_manager import CallConfigManager, SessionManager
from dotenv import load_dotenv
from loguru import logger

from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import (
    EndTaskFrame,
    UserStartedSpeakingFrame, 
    UserStoppedSpeakingFrame
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.openai_llm_context import OpenAILLMContext
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.cartesia.tts import CartesiaTTSService
from pipecat.services.llm_service import FunctionCallParams
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.transports.services.daily import DailyDialinSettings, DailyParams, DailyTransport

load_dotenv(override=True)

logger.remove(0)
logger.add(sys.stderr, level="DEBUG")

daily_api_key = os.getenv("DAILY_API_KEY", "")
daily_api_url = os.getenv("DAILY_API_URL", "https://api.daily.co/v1")


# ------------ HELPER CLASSES ------------

class CallStats:
    """Track call statistics and information."""
    
    def __init__(self):
        self.start_time = datetime.now()
        self.end_time = None
        self.silence_events = 0
        self.silence_prompts = 0
        self.unanswered_prompts = 0
        self.last_user_speech_time = None
        self.longest_silence_duration = 0
        self.total_silence_duration = 0
        self.call_terminated_reason = None
    
    def log_silence_event(self, duration):
        self.silence_events += 1
        self.total_silence_duration += duration
        self.longest_silence_duration = max(self.longest_silence_duration, duration)
    
    def log_silence_prompt(self):
        self.silence_prompts += 1
    
    def log_unanswered_prompt(self):
        self.unanswered_prompts += 1
    
    def end_call(self, reason=None):
        self.end_time = datetime.now()
        self.call_terminated_reason = reason
    
    def get_call_duration(self):
        end = self.end_time or datetime.now()
        return (end - self.start_time).total_seconds()
    
    def get_summary(self):
        duration = self.get_call_duration()
        end = self.end_time or datetime.now()
        
        summary = {
            "call_start": self.start_time.strftime("%Y-%m-%d %H:%M:%S"),
            "call_end": end.strftime("%Y-%m-%d %H:%M:%S"),
            "duration_seconds": round(duration, 2),
            "silence_events": self.silence_events,
            "silence_prompts_sent": self.silence_prompts,
            "unanswered_prompts": self.unanswered_prompts,
            "longest_silence_seconds": round(self.longest_silence_duration, 2),
            "total_silence_seconds": round(self.total_silence_duration, 2),
            "termination_reason": self.call_terminated_reason or "normal"
        }
        
        return summary


class SilenceMonitor(FrameProcessor):
    """Monitor for silence and trigger prompts after extended silence."""
    
    def __init__(self, tts_service, llm_service, call_stats, 
                 silence_threshold_seconds=10.0, max_unanswered_prompts=3):
        super().__init__()
        self.tts_service = tts_service
        self.llm_service = llm_service
        self.call_stats = call_stats
        self.silence_threshold_seconds = silence_threshold_seconds
        self.max_unanswered_prompts = max_unanswered_prompts
        self.last_user_speech_time = time.time()
        self.silence_timer_task = None
        self.user_speaking = False
        self.consecutive_unanswered_prompts = 0
    
    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        
        # Track when user starts/stops speaking
        if isinstance(frame, UserStartedSpeakingFrame):
            self.user_speaking = True
            self.last_user_speech_time = time.time()
            self.consecutive_unanswered_prompts = 0
            self.call_stats.last_user_speech_time = self.last_user_speech_time
            
            # Cancel any pending silence timer
            if self.silence_timer_task and not self.silence_timer_task.done():
                self.silence_timer_task.cancel()
            
        elif isinstance(frame, UserStoppedSpeakingFrame):
            self.user_speaking = False
            # Start the silence timer when user stops speaking
            self.last_user_speech_time = time.time()
            self.call_stats.last_user_speech_time = self.last_user_speech_time
            self.silence_timer_task = asyncio.create_task(self.check_silence())
            
        await self.push_frame(frame, direction)
    
    async def check_silence(self):
        """Check for silence and trigger a prompt if needed."""
        try:
            # Wait until the silence threshold is reached
            await asyncio.sleep(self.silence_threshold_seconds)
            
            # If we reached here, silence threshold was hit
            current_time = time.time()
            silence_duration = current_time - self.last_user_speech_time
            
            # Log the silence event
            self.call_stats.log_silence_event(silence_duration)
            logger.info(f"Silence detected for {silence_duration:.2f} seconds")
            
            # Check if we should terminate due to too many unanswered prompts
            if self.consecutive_unanswered_prompts >= self.max_unanswered_prompts:
                self.call_stats.call_terminated_reason = "max_unanswered_prompts_reached"
                logger.warning(f"Terminating call after {self.consecutive_unanswered_prompts} unanswered prompts")
                
                # Send final message
                final_message = "I haven't heard from you in a while. I'll end this call now. Goodbye!"
                tts_frame = await self.tts_service.synthesize(final_message)
                await self.llm_service.queue_frame(tts_frame, FrameDirection.DOWNSTREAM)
                
                # Wait for TTS to finish
                await asyncio.sleep(5)
                
                # Terminate call
                await self.llm_service.queue_frame(EndTaskFrame(), FrameDirection.UPSTREAM)
                return
            
            # Send a prompt for silence
            self.consecutive_unanswered_prompts += 1
            self.call_stats.log_silence_prompt()
            self.call_stats.log_unanswered_prompt()
            
            prompt_message = "Are you still there? I haven't heard from you for a while."
            tts_frame = await self.tts_service.synthesize(prompt_message)
            await self.llm_service.queue_frame(tts_frame, FrameDirection.DOWNSTREAM)
            
            # Start a new silence timer after sending the prompt
            self.silence_timer_task = asyncio.create_task(self.check_silence())
            
        except asyncio.CancelledError:
            # Timer was cancelled because user started speaking
            pass
        except Exception as e:
            logger.error(f"Error in silence monitor: {e}")


async def main(
    room_url: str,
    token: str,
    body: dict,
):
    # ------------ CONFIGURATION AND SETUP ------------

    # Create a config manager using the provided body
    call_config_manager = CallConfigManager.from_json_string(body) if body else CallConfigManager()

    # Get important configuration values
    test_mode = call_config_manager.is_test_mode()

    # Get dialin settings if present
    dialin_settings = call_config_manager.get_dialin_settings()
    
    # Get enhanced dialin settings
    enhanced_settings = body.get("enhanced_dialin", {}) if body else {}
    silence_threshold = enhanced_settings.get("silenceThresholdSeconds", 10.0)
    max_unanswered_prompts = enhanced_settings.get("maxUnansweredPrompts", 3)

    # Initialize the session manager
    session_manager = SessionManager()
    
    # Initialize call stats
    call_stats = CallStats()

    # ------------ TRANSPORT SETUP ------------

    # Set up transport parameters
    if test_mode:
        logger.info("Running in test mode")
        transport_params = DailyParams(
            api_url=daily_api_url,
            api_key=daily_api_key,
            audio_in_enabled=True,
            audio_out_enabled=True,
            video_out_enabled=False,
            vad_analyzer=SileroVADAnalyzer(),
            transcription_enabled=True,
        )
    else:
        daily_dialin_settings = DailyDialinSettings(
            call_id=dialin_settings.get("call_id"), call_domain=dialin_settings.get("call_domain")
        )
        transport_params = DailyParams(
            api_url=daily_api_url,
            api_key=daily_api_key,
            dialin_settings=daily_dialin_settings,
            audio_in_enabled=True,
            audio_out_enabled=True,
            video_out_enabled=False,
            vad_analyzer=SileroVADAnalyzer(),
            transcription_enabled=True,
        )

    # Initialize transport with Daily
    transport = DailyTransport(
        room_url,
        token,
        "Enhanced Dial-in Bot",
        transport_params,
    )

    # Initialize TTS
    tts = CartesiaTTSService(
        api_key=os.getenv("CARTESIA_API_KEY", ""),
        voice_id="b7d50908-b17c-442d-ad8d-810c63997ed9",  # Use Helpful Woman voice by default
    )

    # ------------ FUNCTION DEFINITIONS ------------

    async def terminate_call(params: FunctionCallParams):
        """Function the bot can call to terminate the call."""
        if session_manager:
            # Mark that the call was terminated by the bot
            session_manager.call_flow_state.set_call_terminated()
        
        # Log call termination in stats
        call_stats.end_call("bot_requested_termination")
        
        # Generate and print call summary
        summary = call_stats.get_summary()
        logger.info("Call Summary:")
        for key, value in summary.items():
            logger.info(f"  {key}: {value}")

        # Then end the call
        await params.llm.queue_frame(EndTaskFrame(), FrameDirection.UPSTREAM)

    # Define function schemas for tools
    terminate_call_function = FunctionSchema(
        name="terminate_call",
        description="Call this function to terminate the call.",
        properties={},
        required=[],
    )

    # Create tools schema
    tools = ToolsSchema(standard_tools=[terminate_call_function])

    # ------------ LLM AND CONTEXT SETUP ------------

    # Set up the system instruction for the LLM
    system_instruction = """You are Chatbot, a friendly, helpful robot. Your goal is to demonstrate your capabilities in a succinct way. Your output will be converted to audio so don't include special characters in your answers. Respond to what the user said in a creative and helpful way, but keep your responses brief. Start by introducing yourself. If the user ends the conversation, **IMMEDIATELY** call the `terminate_call` function. """

    # Initialize LLM
    llm = OpenAILLMService(api_key=os.getenv("OPENAI_API_KEY"))

    # Register functions with the LLM
    llm.register_function("terminate_call", terminate_call)

    # Create system message and initialize messages list
    messages = [call_config_manager.create_system_message(system_instruction)]

    # Initialize LLM context and aggregator
    context = OpenAILLMContext(messages, tools)
    context_aggregator = llm.create_context_aggregator(context)

    # Initialize silence monitor
    silence_monitor = SilenceMonitor(tts, llm, call_stats, silence_threshold, max_unanswered_prompts)

    # ------------ PIPELINE SETUP ------------

    # Build pipeline
    pipeline = Pipeline(
        [
            transport.input(),  # Transport user input
            silence_monitor,    # Monitor for silence
            context_aggregator.user(),  # User responses
            llm,  # LLM
            tts,  # TTS
            transport.output(),  # Transport bot output
            context_aggregator.assistant(),  # Assistant spoken responses
        ]
    )

    # Create pipeline task
    task = PipelineTask(pipeline, params=PipelineParams(allow_interruptions=True))

    # ------------ EVENT HANDLERS ------------

    @transport.event_handler("on_first_participant_joined")
    async def on_first_participant_joined(transport, participant):
        logger.debug(f"First participant joined: {participant['id']}")
        await transport.capture_participant_transcription(participant["id"])
        await task.queue_frames([context_aggregator.user().get_context_frame()])

    @transport.event_handler("on_participant_left")
    async def on_participant_left(transport, participant, reason):
        logger.debug(f"Participant left: {participant}, reason: {reason}")
        # Generate call summary before terminating
        call_stats.end_call("participant_left")
        summary = call_stats.get_summary()
        logger.info("Call Summary:")
        for key, value in summary.items():
            logger.info(f"  {key}: {value}")
        await task.cancel()

    # ------------ RUN PIPELINE ------------

    if test_mode:
        logger.debug("Running in test mode (can be tested in Daily Prebuilt)")

    runner = PipelineRunner()
    await runner.run(task)
    
    # Final call summary (in case it wasn't logged earlier)
    if not call_stats.end_time:
        call_stats.end_call("pipeline_completed")
    summary = call_stats.get_summary()
    logger.info("Final Call Summary:")
    for key, value in summary.items():
        logger.info(f"  {key}: {value}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Enhanced Dial-in Bot")
    parser.add_argument("-u", "--url", type=str, help="Room URL")
    parser.add_argument("-t", "--token", type=str, help="Room Token")
    parser.add_argument("-b", "--body", type=str, help="JSON configuration string")

    args = parser.parse_args()

    # Log the arguments for debugging
    logger.info(f"Room URL: {args.url}")
    logger.info(f"Token: {args.token}")
    logger.info(f"Body provided: {bool(args.body)}")

    asyncio.run(main(args.url, args.token, args.body)) 