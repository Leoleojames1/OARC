"""
This module encapsulates the functionality to convert text into speech using Coqui TTS models.
It handles audio processing, model initialization (either fine-tuned or base with speaker reference),
and manages the generation, playback, and streaming of synthesized audio. The module also provides 
a Gradio interface for testing, robust sentence splitting, user interruption capabilities during audio playback,
and resource cleanup routines to ensure smooth performance.
"""

import sounddevice as sd
import soundfile as sf
import threading
import os
import torch
import re
import queue
import asyncio

from TTS.api import TTS
import numpy as np
import shutil
import time
import keyboard
import json
import websockets

from oarc.utils.log import log
from oarc.utils.paths import Paths

WEBSOCKET_URL = 'ws://localhost:2020/audio-stream'

class TextToSpeech:
    """ a class for managing the text to speech conversation between the user, ollama, & coqui-tts.
    """

    def __init__(self, developer_tools_dict, voice_type, voice_name):
        """
        Initialize the TTS processor.
        
        Args:
            developer_tools_dict: Dictionary containing path configurations
            voice_type: Type of voice to use (e.g., "xtts_v2")
            voice_name: Name of the voice to use (e.g., "c3po")
        """
        # Store initialization parameters
        self.developer_tools_dict = developer_tools_dict
        self.voice_type = voice_type
        self.voice_name = voice_name
        
        # Initialize paths using the Paths singleton
        self.paths = Paths()  # The singleton decorator will handle returning the instance
        
        # Get paths directly from the paths singleton instead of using setup_paths
        tts_paths = self.paths.get_tts_paths_dict()
        self.current_dir = tts_paths['current_dir']
        self.parent_dir = tts_paths['parent_dir']
        self.speech_dir = tts_paths['speech_dir']
        self.recognize_speech_dir = tts_paths['recognize_speech_dir']
        self.generate_speech_dir = tts_paths['generate_speech_dir']
        self.tts_voice_ref_wav_pack_path = tts_paths['tts_voice_ref_wav_pack_path_dir']
        
        # Initialize other properties
        self.tts_model = None
        self.sample_rate = 24000  # Default sample rate for TTS
        self.audio_data = None
        self.is_generating = False
        self.should_interrupt = False
        
        # Initialize the TTS model
        self.initialize_tts_model()

    def initialize_tts_model(self):
        """
        Initialize the appropriate TTS model.
        
        Loads or creates the TTS model based on the specified voice type and name.
        Handles fallbacks and error scenarios gracefully.
        """
        try:
            log.info(f"=========== INITIALIZING TEXT-TO-SPEECH ===========")
            model_git_dir = self.paths.get_model_dir()
            log.info(f"Using model directory: {model_git_dir}")

            # Construct paths
            coqui_dir = os.path.join(model_git_dir, 'coqui')
            if not os.path.exists(coqui_dir):
                os.makedirs(coqui_dir, exist_ok=True)
                log.warning(f"Coqui directory not found, creating: {coqui_dir}")

            # Use the voice reference directory from Paths instead of constructing it manually
            voice_ref_dir = self.paths.get_voice_reference_dir()
            log.info(f"Using voice reference directory: {voice_ref_dir}")

            # List available voices
            available_voices = [d.replace('XTTS-v2_', '') for d in os.listdir(coqui_dir) 
                              if d.startswith('XTTS-v2_') and os.path.isdir(os.path.join(coqui_dir, d))]
            log.info(f"Available voices: {', '.join(available_voices) if available_voices else 'None'}")
            
            fine_tuned_model_path = os.path.join(coqui_dir, f'XTTS-v2_{self.voice_name}')
                
            if os.path.exists(fine_tuned_model_path):
                # Use fine-tuned model
                config_path = os.path.join(fine_tuned_model_path, "config.json")
                model_path = os.path.join(fine_tuned_model_path, "model.pth")
                
                if not os.path.exists(config_path):
                    raise FileNotFoundError(f"Config file not found at {config_path}")
                if not os.path.exists(model_path):
                    raise FileNotFoundError(f"Model file not found at {model_path}")
                    
                log.info(f"Loading fine-tuned model from: {fine_tuned_model_path}")
                self.tts = TTS(
                    model_path=fine_tuned_model_path,
                    config_path=config_path,
                    progress_bar=False,
                    gpu=True
                ).to(self.device)
                self.is_multi_speaker = False
                self.voice_reference_path = os.path.join(fine_tuned_model_path, "reference.wav")
                log.info(f"Loaded fine-tuned model for voice: {self.voice_name}")
                    
            else:
                # Use base model with reference voice
                log.info(f"No fine-tuned model found for {self.voice_name}, using base model with voice reference")
                self.tts = TTS("tts_models/multilingual/multi-dataset/xtts_v2").to(self.device)
                self.is_multi_speaker = True
                    
                # Look for voice reference in voice_reference_pack - use the proper directory
                voice_dir = os.path.join(voice_ref_dir, self.voice_name)
                os.makedirs(voice_dir, exist_ok=True)
                self.voice_reference_path = os.path.join(voice_dir, "clone_speech.wav")
                log.info(f"Looking for voice reference at: {self.voice_reference_path}")
                    
                if not os.path.exists(self.voice_reference_path):
                    # Try reference.wav before raising error
                    alt_ref_path = os.path.join(voice_dir, "reference.wav")
                    if os.path.exists(alt_ref_path):
                        # Copy to clone_speech.wav
                        shutil.copy2(alt_ref_path, self.voice_reference_path)
                        log.info(f"Copied reference.wav to clone_speech.wav for compatibility")
                    else:
                        raise FileNotFoundError(
                            f"Voice reference file not found at {self.voice_reference_path}\n"
                            f"Please ensure voice reference exists at: {voice_dir}\n"
                            f"Available voices in reference pack: {os.listdir(voice_ref_dir) if os.path.exists(voice_ref_dir) else 'None'}"
                        )
                    
            log.info(f"TTS Model initialized successfully on {self.device}")
            log.info(f"=========== TEXT-TO-SPEECH INITIALIZED ===========")
            return True
                
        except Exception as e:
            raise RuntimeError(f"Error initializing TTS model: {str(e)}")
    

    def process_tts_responses(self, response, voice_name):
        """Process text response into audio data suitable for streaming"""
        try:
            # Clear VRAM cache
            torch.cuda.empty_cache()
            
            # Split into sentences
            sentences = self.split_into_sentences(response)
            
            # Clear existing audio buffer
            self.audio_buffer = np.array([], dtype=np.float32)
            
            # Process each sentence
            for sentence in sentences:
                if self.speech_interrupted:
                    break
                    
                # Generate audio
                if self.is_multi_speaker:
                    audio = self.tts.tts(
                        text=sentence,
                        speaker_wav=self.voice_reference_path,
                        language="en",
                        speed=3
                    )
                else:
                    audio = self.tts.tts(
                        text=sentence,
                        language="en",
                        speed=3
                    )
                
                # Convert to float32 numpy array
                audio_np = np.array(audio, dtype=np.float32)
                
                # Normalize audio
                if np.abs(audio_np).max() > 0:
                    audio_np = audio_np / np.abs(audio_np).max()
                
                # Append to buffer
                self.audio_buffer = np.append(self.audio_buffer, audio_np)
            
            # Send audio data to frontend for visualization
            asyncio.run(self.send_audio_to_frontend(self.audio_buffer, "tts"))
            
            # Return the complete audio buffer
            return self.audio_buffer
            
        except Exception as e:
            print(f"Error generating audio: {e}")
            return None


    def play_audio_from_file(self, filename):
        """A method for audio playback from file."""
        # Check if the file exists
        if not os.path.isfile(filename):
            print(f"File {filename} does not exist.")
            return

        try:
            # Load the audio file
            audio_data, sample_rate = sf.read(filename)

            # Play the audio file
            sd.play(audio_data, sample_rate)
            sd.wait()
        except Exception as e:
            print(f"Failed to play audio from file {filename}. Reason: {e}")


    def generate_audio(self, sentence, ticker):
        """ a method to generate the audio for the chatbot
            args: sentence, voice_name_path, ticker
            returns: none
        """
        print(self.colors["LIGHT_CYAN"] + "🔊 generating audio for current sentence chunk 🔊:" + self.colors["RED"])
        # if self.is_multi_speaker:
        tts_audio = self.tts.tts(text=sentence, speaker_wav=self.voice_name_reference_speech_path, language="en", speed=3)
        # else:
        #     tts_audio = self.tts.tts(text=sentence, language="en", speed=3)

        # Convert to NumPy array (adjust dtype as needed)
        tts_audio = np.array(tts_audio, dtype=np.float32)

        # Save the audio with a unique name
        filename = os.path.join(self.generate_speech_dir, f"audio_{ticker}.wav")
        sf.write(filename, tts_audio, 22050)
    

    def get_audio_data(self):
        """Get the current audio buffer in a format suitable for streaming"""
        if len(self.audio_buffer) > 0:
            return {
                "sample_rate": self.sample_rate,
                "data": self.audio_buffer
            }
        return None
    

    def generate_play_audio_loop(self, tts_response_sentences):
        """ a method to generate and play the audio for the chatbot
            args: tts_sentences
            returns: none
        """
        # TODO /interrupt "mode" - added Shut up Feature - during audio playback loop, interupt model and allow user to overwride 
        # chat at current audio out to stop the model from talking and input new speech. 
        # Should probably make it better though, the interrupt loop doesnt function in the nextjs frontend 
        # through the api, it instead functions in the api terminal with hotkeys.
        
        # TODO /input audio "mode" "discord" - add, if modes "spacebar pressed". or "microphone input on" or "smart whisper prompting" with 
        # speech recognized and microphone "silence prompting" all as selections. Also add 2nd arg for discord audio, transcription.
        
        # TODO /decompose "mode" - pipe in Yolo, OCR, screen/game etc data decomposition, 
        # managing what data should be sent through text to speech and what should not
        
        # TODO /cut off speech "mode" - pipe interrupt data to write conversation history and mark/explain 
        # which audio chunk the user cut the model off at with the following modalities;
        #
        # (mode1) have model explain from there (always assume they didnt hear you), or 
        # (mode2) prompt the model with the marked conversation showcasing to the llm model/agent what 
        # the user did not hear through tts, but may have read on the screen text. 
        #
        # These modalities, essentially explaining to the agent wether or not the user can read the text, 
        # or can only hear, for the application of the system. This will provide for more smooth transitions 
        # for conversation, depending on modes 1 & 2.
        
        #TODO /smart listen "feature" - smart whisper/listen podcast moderator and fact checker.
        #
        #   feature set:
        #       wake commands/name call,
        #       long form whisper and audio chunking, storing processing,
        #       presume wether or not you are being spoken to, or listening to others
        #       when others are speaking, do not interrupt them, listen and whisper rec,
        #       while building conversation history, context, links, research, then
        #       forumalate possible response or responses, and when the conversation seems like
        #       both parties have felt they have been respected in their ability
        #       to uphold their free speech, the agent can jump in and say "hey guys! on
        #       that thought maybe you should try this?" 
        #
        #   tools:
        #       feature1: "longform whisper unless namecalled" on/off 
        #       feature2: "combine research data from conversation with /decompose"
        #       feature3: "listen and jump in not based on namecall, but instead based on
        #       respecting the conversation participants, and giving equal opporunities to
        #       contribute to the evolving (podcast) conversation"

        audio_queue = queue.Queue(maxsize=2)  # Queue to hold generated audio
        interrupted = False
        generation_complete = False


        def generate_audio_thread():
            nonlocal generation_complete
            for i, sentence in enumerate(tts_response_sentences):
                if interrupted:
                    break
                self.generate_audio(sentence, i)
                audio_queue.put(i)
            generation_complete = True

        # Start the audio generation thread
        threading.Thread(target=generate_audio_thread, daemon=True).start()

        ticker = 0
        while not (generation_complete and audio_queue.empty()) and not interrupted:
            try:
                current_ticker = audio_queue.get(timeout=0.1)
                filename = os.path.join(self.generate_speech_dir, f"audio_{current_ticker}.wav")
                
                play_thread = threading.Thread(target=self.play_audio_from_file, args=(filename,))
                play_thread.start()

                while play_thread.is_alive():
                    if keyboard.is_pressed('shift+alt'):
                        sd.stop()  # Stop the currently playing audio
                        interrupted = True
                        break
                    time.sleep(0.1)  # Small sleep to prevent busy-waiting

                play_thread.join()
                ticker += 1

            except queue.Empty:
                continue  # If queue is empty, continue waiting

        if interrupted:
            print(self.colors["BRIGHT_YELLOW"] + "Speech interrupted by user." + self.colors["RED"])
            self.clear_remaining_audio_files(ticker, len(tts_response_sentences))


    def interrupt_generation(self):
        """Interrupt ongoing speech generation"""
        self.speech_interrupted = True
        self.audio_buffer = np.array([], dtype=np.float32)
        

    def cleanup(self):
        """Clean up resources"""
        self.speech_interrupted = True
        self.audio_buffer = np.array([], dtype=np.float32)
        torch.cuda.empty_cache()
        

    def clear_remaining_audio_files(self, start_ticker, total_sentences):
        """ a method to clear the audio cache from the current splicing session
        """
        for i in range(start_ticker, total_sentences):
            filename = os.path.join(self.generate_speech_dir, f"audio_{i}.wav")
            if os.path.exists(filename):
                os.remove(filename)


    def clear_directory(self, directory):
        """ a method for clearing the given directory
            args: directory
            returns: none
        """
        for filename in os.listdir(directory):
            file_path = os.path.join(directory, filename)
            try:
                if os.path.isfile(file_path) or os.path.islink(file_path):
                    os.unlink(file_path)
                elif os.path.isdir(file_path):
                    shutil.rmtree(file_path)
            except Exception as e:
                print(f'Failed to delete {file_path}. Reason: {e}')


    def split_into_sentences(self, text: str) -> list[str]:
        """A method for splitting the LLAMA response into sentences.
        Args:
            text (str): The input text.
        Returns:
            list[str]: List of sentences.

            #TODO split by * * as well, for roleplay text such as *ahem*, *nervous laughter*, etc. when this
            is sent to a diffusion model such as bark or f5, the tts will process them as emotional sounds,
            as well as handling them as seperate chunks split from the rest which will increase speed.
            
            #TODO retrain c3po with shorter sentences and more even volume distribution

            #TODO maximum split must be less than 250 token
                - no endless sentences, -> blocks of 11 seconds, if more the model will speed up to fit it in 
                that space where you control multiple generations, instead split out chunks and handle properly.
        """
        # Add spaces around punctuation marks for consistent splitting
        text = " " + text + " "
        text = text.replace("\n", " ")

        # Handle common abbreviations and special cases
        text = re.sub(r"(Mr|Mrs|Ms|Dr|i\.e)\.", r"\1<prd>", text)
        text = re.sub(r"\.\.\.", r"<prd><prd><prd>", text)

        # Split on period, question mark, exclamation mark, or colon followed by optional spaces
        sentences = re.split(r"(?<=\d\.)\s+|(?<=[.!?:])\s+", text)

        # Remove empty sentences
        sentences = [s.strip() for s in sentences if s.strip()]

        # Combine the number with its corresponding sentence
        combined_sentences = []
        i = 0
        while i < len(sentences):
            if re.match(r"^\d+\.", sentences[i]):
                combined_sentences.append(f"{sentences[i]} {sentences[i + 1]}")
                i += 2
            else:
                combined_sentences.append(sentences[i])
                i += 1

        # Ensure sentences are no longer than 250 characters
        final_sentences = []
        for sentence in combined_sentences:
            while len(sentence) > 250:
                # Find the nearest space before the 250th character
                split_index = sentence.rfind(' ', 0, 249)
                if split_index == -1:  # No space found, force split at 249
                    split_index = 249
                final_sentences.append(sentence[:split_index].strip())
                sentence = sentence[split_index:].strip()
            final_sentences.append(sentence)

        return final_sentences
    

    def gradio_interface(self):
        """Create Gradio interface for the detector"""
        import gradio as gr
        import numpy as np
        import sounddevice as sd
        import soundfile as sf

        def tts(text: str):
            # Process text
            audio_data = self.process_tts_responses(text, self.voice_name)
            if audio_data is not None:
                # Play audio
                sd.play(audio_data, self.sample_rate)
                sd.wait()
            return "Audio playback complete"

        gr.Interface(fn=tts, inputs="text", outputs="text").launch()


    async def send_audio_to_frontend(self, audio_data, audio_type):
        async with websockets.connect(WEBSOCKET_URL) as websocket:
            await websocket.send(json.dumps({
                'audio_type': audio_type,
                'audio_data': list(audio_data)
            }))
