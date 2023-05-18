import asyncio
import websockets
import pickle, struct, time, pyaudio
import threading
import os
import wave
import textwrap
from collections import deque
from dataclasses import dataclass

import torch
import numpy as np

from transcriber import WhisperModel


@dataclass(frozen=True)
class Constants:
    AUDIO_OVER = b"audio_data_over"
    ACK = b"acknowledged"
    SENDING_FILE = b"sending_audio_file"
    FILE_SENT = b"audio_file_sent"

frames_np = None
frames_offset = 0.0
RATE = 16000
clients = {}
client_ids = {}
connections = []


async def recv_audio(websocket):
    """
    Receive audio chunks from client in an infinite loop.
    """
    global frames_np, frames_offset, RATE
    connections.append(websocket)
    client = ServeClient()
    asyncio.ensure_future(client.speech_to_text())
    data = b''
    while True:
        try:
            frame_data = await websocket.recv()
            if isinstance(frame_data, str):
                continue
            frame_np = np.fromstring(frame_data, np.float32)
            if frames_np is not None and frames_np.shape[0] > 45*RATE:
                frames_offset += 45.0
                frames_np = frames_np[int(30*RATE):]
            if frames_np is None:
                frames_np = frame_np.copy()
            else:
                frames_np = np.concatenate((frames_np, frame_np), axis=0)
            print(frames_np.shape[0] / RATE)
        except websockets.ConnectionClosedOK:
            print("Connection Closed.")
            break
        await asyncio.sleep(0.00001)


class ServeClient:
    RATE = 16000
    def __init__(self, websocket=None, topic=None, device=None, verbose=True):
        self.payload_size = struct.calcsize("Q")
        self.data = b""
        self.frames = b""
        self.transcriber = WhisperModel("small.en", device="cuda", compute_type="float16")
        self.timestamp_offset = 0.0
        self.text = []
        self.current_out = ''
        self.prev_out = ''
        self.t_start=None
        self.verbose = verbose
        self.exit = False
        self.same_output_threshold = 0
        self.show_prev_out_thresh = 5   # if pause(no output from whisper) show previous output for 5 seconds
        self.add_pause_thresh = 3       # add a blank to segment list as a pause(no speech) for 3 seconds

        # text formatting
        self.wrapper = textwrap.TextWrapper(width=50)
        self.pick_previous_segments = 2

        # setup mqtt
        self.topic = topic

        # threading
        self.websocket = websocket
    
    def send_response_to_client(self, message):
        """
        Send serialized response to client.
        """
        a = pickle.dumps(message)
        message = struct.pack("Q",len(a))+a
        self.client_socket.sendall(message)
    
    def fill_output(self, output):
        """
        Format output with current and previous complete segments
        into two lines of 50 characters.

        Args:
            output(str): current incomplete segment
        
        Returns:
            transcription wrapped in two lines
        """
        text = ''
        pick_prev = min(len(self.text), self.pick_previous_segments)
        for seg in self.text[-pick_prev:]:
            # discard everything before a 3 second pause
            if seg == '':
                text = ''
            else:
                text += seg
        wrapped = self.wrapper.wrap(
            text="".join(text + output))[-2:]
        return " ".join(wrapped)

    async def speech_to_text(self):
        """
        Process audio stream in an infinite loop.
        """
        global frames_np, frames_offset, connections

        while True:
            if self.exit: 
                self.mqttc.disconnect()
                self.transcriber.destroy()
                break
            if frames_np is None or len(connections)==0: 
                await asyncio.sleep(0.01)
                continue
            self.websocket = connections[0]

            # clip audio if the current chunk exceeds 25 seconds, this basically implies that
            # no valid segment for the last 25 seconds from whisper
            if frames_np[int((self.timestamp_offset - frames_offset)*self.RATE):].shape[0] > 25 * self.RATE:
                duration = frames_np.shape[0] / self.RATE
                self.timestamp_offset = frames_offset + duration - 5
    
            samples_take = max(0, (self.timestamp_offset - frames_offset)*self.RATE)
            input_bytes = frames_np[int(samples_take):].copy()
            duration = input_bytes.shape[0] / self.RATE
            if duration<1.0: 
                await asyncio.sleep(0.01)
                continue
            try:
                input_sample = input_bytes.copy()
                # set previous complete segment as initial prompt
                if len(self.text) and self.text[-1] != '': 
                    initial_prompt = self.text[-1]
                else: 
                    initial_prompt = None
                # whisper transcribe with prompt
                result = self.transcriber.transcribe(input_sample, initial_prompt=initial_prompt)
                if len(result):
                    self.t_start = None
                    output, segments = self.update_segments(result, duration)
                    out_dict = {
                        'text': output,
                        'segments': segments
                    }

                    await self.websocket.send(output)
                else:
                    # show previous output if there is pause i.e. no output from whisper
                    output = ''
                    if self.t_start is None: self.t_start = time.time()
                    if time.time() - self.t_start < self.show_prev_out_thresh:
                        output = self.fill_output('')
                    # add a blank if there is no speech for 3 seconds
                    if len(self.text) and self.text[-1] != '':
                        if time.time() - self.t_start > self.add_pause_thresh:
                            self.text.append('')
                    # publish outputs
                    out_dict = {
                        'text': output,
                        'segments': []
                    }
     
                    await self.websocket.send(output)
            except Exception as e:
                if self.verbose: print(f"[ERROR]: {e}")
                time.sleep(0.01)
            await asyncio.sleep(0.00001)
    
    def update_segments(self, segments, duration):
        """
        Processes the segments from whisper. Appends all the segments to the list
        except for the last segment assuming that it is incomplete.

        Args:
            segments(dict) : dictionary of segments as returned by whisper
            duration(float): duration of the current chunk
        
        Returns:
            transcription for the current chunk
        """
        offset = None
        transcript = []
        self.current_out = ''
        # process complete segments
        if len(segments) > 1:
            for i, s in enumerate(segments[:-1]):
                text_ = s.text
                self.text.append(text_)
                start, end = self.timestamp_offset + s.start, self.timestamp_offset + min(duration, s.end)
                transcript.append(
                    {
                        'start': start,
                        'end': end,
                        'text': text_
                    }
                )
                
                offset = min(duration, s.end)

        self.current_out += segments[-1].text
        
        # if same incomplete segment is seen multiple times then update the offset
        # and append the segment to the list
        if self.current_out.strip() == self.prev_out.strip() and self.current_out != '': 
            self.same_output_threshold += 1
        else: 
            self.same_output_threshold = 0
        
        if self.same_output_threshold > 5:
            if not len(self.text) or self.text[-1].strip().lower()!=self.current_out.strip().lower():          
                self.text.append(self.current_out)
                transcript.append(
                    {
                        'start': self.timestamp_offset,
                        'end': self.timestamp_offset + duration,
                        'text': self.current_out
                    }
                )
            self.current_out = ''
            offset = duration
            self.same_output_threshold = 0
        else:
            self.prev_out = self.current_out
        
        # update offset
        if offset is not None:
            self.timestamp_offset += offset

        # format and return output
        output = self.current_out
        return self.fill_output(output), transcript
    

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(websockets.serve(recv_audio, "", 9090, ping_interval=None))
    loop.run_forever()