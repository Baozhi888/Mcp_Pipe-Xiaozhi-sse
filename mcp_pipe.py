"""
This script is used to connect to the MCP server and pipe the input and output to the websocket endpoint.
Version: 0.3.0
Author: PonYoung
Date: 2025-05-25
LastEditors: PonYoung
LastEditTime: 2025-05-25 
Description: 使用主流（SSE/STDIO/Streamable HTTP）方式启用小智MCP
====================== 声明 ====================
注意：仅用于学习目的。请勿将其用于商业用途！
================================================
Usage:

export MCP_ENDPOINT=<mcp_endpoint>
python mcp_pipe.py <mcp_script>

Or use a config file:
python mcp_pipe.py config.yaml

Optional arguments:
  --debug    Enable debug logging
"""

import asyncio
import websockets
import subprocess
import logging
import os
import signal
import sys
import random
import yaml
import aiohttp
import json
import argparse
from dotenv import load_dotenv
from urllib.parse import urlparse

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('MCP_PIPE')

INITIAL_BACKOFF = 1
MAX_BACKOFF = 600
reconnect_attempt = 1
backoff = INITIAL_BACKOFF

shttp_last_event_ids = {}

# 响应队列类
class ResponseQueue:
    def __init__(self, maxsize=1000):
        """Initialize response queue with size limit and cleanup settings
        
        Args:
            maxsize (int): Maximum number of items in queue before blocking
        """
        self.queue = asyncio.Queue(maxsize=maxsize)
        self.tool_requests = {}
        self.tool_request_timestamps = {}
        self.tool_timeout = 300
        self._cleanup_task = None
        self._running = True
        self._closed = False
        
    async def start(self):
        """Start the cleanup task"""
        self._closed = False
        self._running = True
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        
    async def stop(self):
        """Stop the cleanup task"""
        self._running = False
        self._closed = True
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
        # Clear any remaining items
        try:
            while not self.queue.empty():
                self.queue.get_nowait()
        except asyncio.QueueEmpty:
                pass
        
    async def add(self, message):
        """Add message to queue with timeout
        
        Args:
            message: Message to add to queue
            
        Raises:
            asyncio.QueueFull: If queue is full and timeout occurs
            Exception: For other errors during queue operation
        """
        if self._closed:
            return
        try:
            await asyncio.wait_for(self.queue.put(message), timeout=10.0)
        except asyncio.QueueFull:
            logger.error("Response queue is full, dropping message")
            raise
        except asyncio.TimeoutError:
            logger.error("Timeout while adding message to queue")
            raise
        except Exception as e:
            logger.error(f"Error adding message to queue: {e}")
            raise
        
    async def get(self):
        """Get message from queue with timeout
        
        Returns:
            Message from queue
            
        Raises:
            asyncio.TimeoutError: If timeout occurs while waiting
            Exception: For other errors during queue operation
        """
        if self._closed:
            raise asyncio.CancelledError("Queue is closed")
        try:
            return await asyncio.wait_for(self.queue.get(), timeout=60.0)  # Increased timeout
        except asyncio.TimeoutError:
            if not self._closed:  # Only log warning if queue is not intentionally closed
                logger.warning("Timeout while getting message from queue")
            raise
        except Exception as e:
            logger.error(f"Error getting message from queue: {e}")
            raise
        
    def register_tool_request(self, request_id, name):
        """Register a tool request with timestamp
        
        Args:
            request_id: Request ID
            name: Tool name
        """
        current_time = asyncio.get_event_loop().time()
        self.tool_requests[request_id] = name
        self.tool_request_timestamps[request_id] = current_time
        logger.debug(f"Registered tool request {request_id} ({name}) at {current_time}")
        
    def get_tool_request(self, response_id):
        """Get and remove tool request if exists
        
        Args:
            response_id: Response ID to match with request
            
        Returns:
            Tool name if found, None otherwise
        """
        tool_name = self.tool_requests.pop(response_id, None)
        if tool_name:
            self.tool_request_timestamps.pop(response_id, None)
            logger.debug(f"Retrieved and removed tool request {response_id} ({tool_name})")
        return tool_name
        
    async def _cleanup_loop(self):
        """Periodically clean up expired tool requests"""
        while self._running:
            try:
                current_time = asyncio.get_event_loop().time()
                expired_requests = [
                    req_id for req_id, timestamp in self.tool_request_timestamps.items()
                    if current_time - timestamp > self.tool_timeout
                ]
                
                for req_id in expired_requests:
                    tool_name = self.tool_requests.pop(req_id, None)
                    self.tool_request_timestamps.pop(req_id, None)
                    if tool_name:
                        logger.warning(f"Cleaned up expired tool request {req_id} ({tool_name})")
                
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in cleanup loop: {e}")
                await asyncio.sleep(60)
        
    @property
    def queue_size(self):
        """Get current queue size"""
        return self.queue.qsize()
        
    @property
    def pending_tool_requests(self):
        """Get number of pending tool requests"""
        return len(self.tool_requests)

response_queue = ResponseQueue()

def set_debug_level(debug=False):
    if debug:
        logger.setLevel(logging.DEBUG)
        logger.debug("Debug logging enabled")
    else:
        logger.setLevel(logging.INFO)

async def connect_with_retry(uri, target, mode='stdio'):
    """Connect to WebSocket server with retry mechanism"""
    global reconnect_attempt, backoff
    while True:
        try:
            if reconnect_attempt > 0:
                wait_time = backoff * (1 + random.random() * 0.1)
                logger.info(f"Waiting {wait_time:.2f} seconds before reconnection attempt {reconnect_attempt}...")
                await asyncio.sleep(wait_time)
                
            # Attempt to connect
            await connect_to_server(uri, target, mode)
        
        except Exception as e:
            reconnect_attempt += 1
            logger.warning(f"Connection closed (attempt: {reconnect_attempt}): {e}")        
            backoff = min(backoff * 2, MAX_BACKOFF)

async def connect_to_server(uri, target, mode='stdio'):
    """Connect to WebSocket server and establish bidirectional communication with target"""
    global reconnect_attempt, backoff, response_queue
    
    # Ensure old response queue is properly cleaned up
    if response_queue:
        await response_queue.stop()
    
    response_queue = ResponseQueue()
    logger.info("Response queue re-initialized for new connection.")
    
    await response_queue.start()
    logger.info("Response queue cleanup task started.")
    
    if hasattr(pipe_websocket_to_sse, 'endpoint'):
        pipe_websocket_to_sse.endpoint = None
    if hasattr(pipe_streamable_http, 'endpoint'):
        pipe_streamable_http.endpoint = None
        
    try:
        logger.info(f"Connecting to WebSocket server...")
        async with websockets.connect(uri) as websocket:
            logger.info(f"Successfully connected to WebSocket server")
            
            reconnect_attempt = 0
            backoff = INITIAL_BACKOFF
            
            response_processor = asyncio.create_task(process_response_queue(websocket))
            
            try:
                if mode == 'stdio':
                    process = subprocess.Popen(
                        ['python', target],
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        encoding='utf-8',
                        errors='replace'
                    )
                    logger.info(f"Started {target} process")
                    
                    await asyncio.gather(
                        pipe_websocket_to_process(websocket, process),
                        pipe_process_to_queue(process),
                        pipe_process_stderr_to_terminal(process),
                        response_processor
                    )
                elif mode == 'sse':
                    logger.info(f"Starting SSE mode with endpoint: {target}")
                    
                    async with aiohttp.ClientSession() as session:
                        try:
                            async with session.get(target) as sse_response:
                                if sse_response.status != 200:
                                    logger.error(f"Failed to connect to SSE endpoint: {sse_response.status}")
                                    return
                                    
                                logger.info("Connected to SSE endpoint successfully")
                                
                                base_url = target.split('/sse')[0]
                                
                                await asyncio.gather(
                                    pipe_websocket_to_sse(websocket, session, base_url),
                                    pipe_sse_to_websocket(sse_response, websocket),
                                    response_processor
                                )
                        except Exception as e:
                            logger.error(f"SSE connection error: {e}")
                            raise
                elif mode == 'streamable_http':
                    logger.info(f"Starting Streamable HTTP mode with endpoint: {target}")
                    
                    async with aiohttp.ClientSession() as session:
                        await pipe_streamable_http(websocket, session, target)
                else:
                    logger.error(f"Unsupported mode: {mode}")
                    response_processor.cancel()
                    return
            finally:
                if response_processor and not response_processor.done():
                    response_processor.cancel()
                    try:
                        await response_processor
                    except asyncio.CancelledError:
                        pass
    except websockets.exceptions.ConnectionClosed as e:
        logger.error(f"WebSocket connection closed: {e}")
        raise
    except Exception as e:
        logger.error(f"Connection error: {e}")
        raise
    finally:
        await response_queue.stop()
        logger.info("Response queue cleanup task stopped.")
        
        if mode == 'stdio' and 'process' in locals():
            logger.info(f"Terminating {target} process")
            try:
                process.terminate()
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
            logger.info(f"{target} process terminated")

async def pipe_websocket_to_process(websocket, process):
    """Read data from WebSocket and write to process stdin"""
    try:
        while True:
            message = await websocket.recv()
            
            if isinstance(message, bytes):
                message = message.decode('utf-8')
            process.stdin.write(message + '\n')
            process.stdin.flush()
    except Exception as e:
        logger.error(f"Error in WebSocket to process pipe: {e}")
        raise
    finally:
        if not process.stdin.closed:
            process.stdin.close()

async def pipe_process_to_queue(process):
    """Read data from process stdout and send to queue"""
    try:
        while True:
            data = await asyncio.get_event_loop().run_in_executor(
                None, process.stdout.readline
            )
            
            if not data:
                logger.info("Process has ended output")
                break
                
            # Send data to queue
            await response_queue.add(data)
    except Exception as e:
        logger.error(f"Error in process to queue pipe: {e}")
        raise

async def pipe_process_stderr_to_terminal(process):
    """Read data from process stderr and print to terminal"""
    try:
        while True:
            data = await asyncio.get_event_loop().run_in_executor(
                None, process.stderr.readline
            )
            
            if not data:
                logger.info("Process has ended stderr output")
                break
                
            sys.stderr.write(data)
            sys.stderr.flush()
    except Exception as e:
        logger.error(f"Error in process stderr pipe: {e}")
        raise

async def pipe_websocket_to_sse(websocket, session, base_url):
    """Read data from WebSocket and send to SSE server via POST"""
    message_endpoint = None
    session_id = None
    
    while message_endpoint is None:
        if hasattr(pipe_websocket_to_sse, 'endpoint') and pipe_websocket_to_sse.endpoint:
            message_endpoint = pipe_websocket_to_sse.endpoint
            break
        await asyncio.sleep(0.1)
    
    logger.info(f"Using message endpoint: {message_endpoint}")
    
    session_part = ""
    if '?' in message_endpoint:
        path_part, session_part = message_endpoint.split('?', 1)
        if '/message' in path_part:
            path_part = '/message'
    else:
        path_part = message_endpoint
        
    if base_url.endswith('/'):
        base_url = base_url[:-1]
    
    if not path_part.startswith('/'):
        path_part = '/' + path_part
        
    if session_part:
        full_endpoint = f"{base_url}{path_part}?{session_part}"
    else:
        full_endpoint = f"{base_url}{path_part}"
            
    logger.info(f"Constructed full endpoint: {full_endpoint}")
    
    session_id = await initialize_session(session, full_endpoint)
    if session_id:
        logger.info(f"SSE mode initialized with session ID: {session_id}")
    else:
        logger.warning("SSE mode: No session ID received from initialize_session.")

    heartbeat_task = asyncio.create_task(send_heartbeat(session, full_endpoint, session_id))
    
    try:
        while True:
            message = await websocket.recv()
            
            try:
                msg_data = json.loads(message)
                if ('method' in msg_data and msg_data['method'] == 'tools/call' and 
                    'params' in msg_data and 'name' in msg_data['params']):
                    tool_name = msg_data['params']['name']
                    request_id = msg_data.get('id')
                    
                    response_queue.register_tool_request(request_id, tool_name)
                    logger.info(f"Routing tool '{tool_name}' to SSE handler")
            except Exception as e:
                pass
            
            if isinstance(message, bytes):
                message = message.decode('utf-8')
                
            try:
                logger.info(f"Sending message to: {full_endpoint}")
                
                headers = {"Content-Type": "application/json"}
                
                if not message.startswith('{'):
                    message = json.dumps({"message": message})
                
                async with session.post(full_endpoint, data=message, headers=headers) as response:
                    if response.status not in [200, 202]:
                        logger.warning(f"Failed to send message to SSE server: Status {response.status}")
                        response_text = await response.text()
                        logger.warning(f"Error response: {response_text[:200]}")
                    else:
                        logger.info(f"Successfully sent message to SSE server: Status {response.status}")
            except Exception as e:
                logger.error(f"Error sending message to SSE server: {e}")
    except Exception as e:
        logger.error(f"Error in WebSocket to SSE pipe: {e}")
        raise
    finally:
        if heartbeat_task and not heartbeat_task.done():
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass

async def pipe_sse_to_websocket(sse_response, websocket):
    """Read data from SSE response and send to WebSocket"""
    try:
        logger.info("Starting to read SSE events...")
        event_type = None
        data_buffer = []
        
        async for line in sse_response.content:
            line = line.decode('utf-8').strip()
            logger.debug(f"SSE raw line: '{line}'")
            
            if not line:
                if data_buffer and event_type:
                    full_data = ''.join(data_buffer)
                    logger.info(f"SSE {event_type} event received, length: {len(full_data)}")
                    
                    if event_type == 'endpoint':
                        logger.info(f"Received endpoint: {full_data}")
                        pipe_websocket_to_sse.endpoint = full_data
                    elif event_type == 'message':
                        try:
                            data_obj = json.loads(full_data)
                            logger.debug(f"Parsed message data: {data_obj}")
                            
                            actual_message = full_data
                            if isinstance(data_obj, dict) and 'message' in data_obj:
                                actual_message = data_obj['message']
                                if isinstance(actual_message, dict):
                                    actual_message = json.dumps(actual_message)
                                logger.info("Extracted message from wrapper")
                            
                            if isinstance(data_obj, dict) and 'result' in data_obj and \
                               isinstance(data_obj['result'], dict) and 'tools' in data_obj['result']:
                                logger.info(f"Received tools list with {len(data_obj['result']['tools'])} tools")
                            
                            if isinstance(data_obj, dict) and 'id' in data_obj:
                                response_id = data_obj['id']
                                tool_name = response_queue.get_tool_request(response_id)
                                if tool_name:
                                    logger.info(f"Received response for tool '{tool_name}'")
                            
                            await response_queue.add(actual_message)
                        except json.JSONDecodeError:
                            logger.warning(f"Received non-JSON message from SSE: {full_data[:50]}...")
                            await response_queue.add(full_data)
                        except Exception as e:
                            logger.warning(f"Error processing SSE message: {e}")
                            await response_queue.add(full_data)
                
                event_type = None
                data_buffer = []
                continue
                
            if line.startswith('event:'):
                event_type = line[6:].strip()
                continue
                
            if line.startswith('data:'):
                data = line[5:].strip()
                data_buffer.append(data)
                continue
    except Exception as e:
        logger.error(f"Error in SSE to WebSocket pipe: {e}")
        raise

async def process_response_queue(websocket):
    """处理响应队列，将响应发送到WebSocket"""
    try:
        logger.info("Started response queue processor")
        while True:
            response = await response_queue.get()
            
            if isinstance(response, str):
                if response.startswith('event:') or response.startswith('data:'):
                    try:
                        if 'data:' in response:
                            data = response.split('data:', 1)[1].strip()
                            try:
                                json_data = json.loads(data)
                                response = json.dumps(json_data)
                            except json.JSONDecodeError:
                                response = data
                    except Exception as e:
                        logger.warning(f"Error processing SSE data: {e}")
                        continue
            
            response_type = "Unknown"
            try:
                if isinstance(response, str) and response.startswith('{'):
                    data = json.loads(response)
                    if 'method' in data:
                        response_type = f"Method: {data['method']}"
                    elif 'result' in data and isinstance(data['result'], dict) and 'tools' in data['result']:
                        response_type = f"Tools list ({len(data['result']['tools'])} tools)"
                        logger.info(f"Found tools list with {len(data['result']['tools'])} tools")
                    elif 'result' in data:
                        response_type = "Tool result"
                    elif 'error' in data:
                        response_type = f"Error: {data.get('error', {}).get('message', 'Unknown error')}"
                    else:
                        response_type = "JSON data"
            except json.JSONDecodeError:
                pass
                
            logger.info(f"Sending to WebSocket: {response_type} ({len(response) if isinstance(response, str) else 'non-string'} bytes)")
            logger.debug(f"Response content: {response[:200]}..." if isinstance(response, str) and len(response) > 200 else response)
            
            try:
                await asyncio.wait_for(websocket.send(response), timeout=20.0)
            except websockets.exceptions.ConnectionClosed as e:
                logger.error(f"WebSocket connection closed while sending response: {e}")
                raise
            except asyncio.TimeoutError:
                logger.warning("Timeout occurred while sending response to WebSocket. Forcing reconnect.")
                raise websockets.exceptions.ConnectionClosed(None, "WebSocket send timeout")
            except Exception as e:
                logger.error(f"Error sending response to WebSocket: {e}. Forcing reconnect.")
                raise websockets.exceptions.ConnectionClosed(None, f"WebSocket send error: {e}")
                
    except asyncio.CancelledError:
        logger.info("Response queue processor cancelled")
        raise
    except Exception as e:
        logger.error(f"Error processing response queue: {e}")
        raise

async def pipe_streamable_http(websocket, session, base_url):
    """Handle Streamable HTTP communication"""
    session_id = None
    http_heartbeat_task = None
    ws_heartbeat_task = None
    request_queue = asyncio.Queue()
    
    async def handle_requests():
        """处理从WebSocket接收消息并放入请求队列的协程"""
        while True:
            try:
                message = await websocket.recv()
                await request_queue.put(message)
            except websockets.exceptions.ConnectionClosed:
                logger.info("SHTTP: WebSocket connection closed while handling requests.")
                break
            except Exception as e:
                logger.error(f"SHTTP: Error receiving WebSocket message: {e}")
                break 
                
    async def process_requests(current_endpoint_key):
        """处理请求队列中的消息，发送HTTP POST并处理流式响应的协程"""
        nonlocal session_id
        active_requests = set()
        
        async def handle_single_request(message):
            nonlocal session_id  # 确保在内部函数中也可以访问session_id
            try:
                try:
                    msg_data = json.loads(message)
                    if ('method' in msg_data and msg_data['method'] == 'tools/call' and 
                        'params' in msg_data and 'name' in msg_data['params']):
                        tool_name = msg_data['params']['name']
                        request_id = msg_data.get('id')
                        response_queue.register_tool_request(request_id, tool_name)
                        logger.info(f"SHTTP: Routing tool '{tool_name}' to Streamable HTTP handler")
                except json.JSONDecodeError:
                    logger.warning("SHTTP: Failed to parse WebSocket message as JSON for tool registration")

                headers = {
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream"
                }
                
                if session_id:
                    headers["Mcp-Session-Id"] = session_id
                    logger.debug(f"SHTTP: Using session ID: {session_id}")
                else:
                    logger.warning("SHTTP: No session ID available for request")

                last_id_for_header = shttp_last_event_ids.get(current_endpoint_key)
                if last_id_for_header:
                    try:
                        parsed_msg = json.loads(message)
                        if parsed_msg.get("method") not in ["tools/list", "ping", "initialize", "session/terminate"]:
                            headers["Last-Event-ID"] = last_id_for_header
                            logger.info(f"SHTTP: Sending message with Last-Event-ID: {last_id_for_header}")
                    except Exception:
                        pass

                data_to_send = message if isinstance(message, str) else message.decode('utf-8')
                if not data_to_send.startswith('{'): 
                    data_to_send = json.dumps({"message": data_to_send})

                logger.info(f"SHTTP: Sending POST to: {current_endpoint_key}")
                async with session.post(current_endpoint_key, data=data_to_send, headers=headers) as response:
                    if response.status not in [200, 202]:
                        error_text = await response.text()
                        logger.error(f"SHTTP: Server error {response.status}: {error_text}")
                        if response.status == 4004:
                            await websocket.close(code=4004, reason="Server internal error (4004)")
                        return

                    new_session_id = response.headers.get('Mcp-Session-Id')
                    if new_session_id:
                        session_id = new_session_id
                        logger.info(f"SHTTP: Updated Mcp-Session-Id to: {session_id}")
                    elif not session_id:
                        logger.warning("SHTTP: Still no session ID after request")

                    logger.info(f"SHTTP: Successfully sent message and received response")
                    
                    response_content_buffer = ""
                    async for line_bytes in response.content:
                        line_str = line_bytes.decode('utf-8')
                        response_content_buffer += line_str
                        
                        while '\n\n' in response_content_buffer:
                            event_block, response_content_buffer = response_content_buffer.split('\n\n', 1)
                            if not event_block.strip():
                                continue
                                
                            current_event_id = None
                            data_lines = []
                            
                            for event_line in event_block.split('\n'):
                                line = event_line.strip()
                                if line.startswith('id:'):
                                    current_event_id = line[3:].strip()
                                elif line.startswith('data:'):
                                    data_lines.append(line[5:].strip())
                                    
                            if current_event_id:
                                shttp_last_event_ids[current_endpoint_key] = current_event_id
                                
                            if data_lines:
                                full_data = '\n'.join(data_lines)
                                try:
                                    json_data = json.loads(full_data)
                                    if 'error' in json_data:
                                        logger.error(f"SHTTP: Server error in stream: {json_data['error']}")
                                        if json_data.get('code') == 4004:
                                            await websocket.close(code=4004, reason=str(json_data['error']))
                                            return
                                            
                                    await response_queue.add(full_data)
                                except json.JSONDecodeError:
                                    await response_queue.add(full_data)
                                except Exception as e:
                                    logger.error(f"SHTTP: Error processing response: {e}")

            except asyncio.CancelledError:
                logger.info("SHTTP: Request handling cancelled")
                raise
            except Exception as e:
                logger.error(f"SHTTP: Error handling request: {e}")
                if '4004' in str(e):
                    await websocket.close(code=4004, reason="Server error (4004)")
                    raise
        
        while True:
            try:
                message = await request_queue.get()
                if message is None:
                    break
                    
                # 清理已完成的请求
                active_requests = {task for task in active_requests if not task.done()}
                
                # 创建新的请求处理任务
                task = asyncio.create_task(handle_single_request(message))
                active_requests.add(task)
                
            except asyncio.CancelledError:
                logger.info("SHTTP: Process requests task cancelled")
                break
            except Exception as e:
                logger.error(f"SHTTP: Error in main request loop: {e}")
                await asyncio.sleep(1)
                continue
                
        # 等待所有活跃请求完成
        if active_requests:
            await asyncio.gather(*active_requests, return_exceptions=True)

    try:
        endpoint = base_url.rstrip('/')
        logger.info(f"SHTTP: Mode starting with target endpoint: {endpoint}")
        
        # 初始化session并获取session_id
        session_id = await initialize_session(session, endpoint)
        if session_id:
            logger.info(f"SHTTP: Initialized with Mcp-Session-Id: {session_id}")
        else:
            logger.warning("SHTTP: No Mcp-Session-Id received from initialize_session.")
            # 继续执行，因为某些请求可能不需要session_id
        
        # Callable to get the current session_id for heartbeat
        def get_shttp_session_id():
            return session_id

        http_heartbeat_task = asyncio.create_task(
            send_heartbeat(session, endpoint, get_shttp_session_id)
        )
        ws_heartbeat_task = asyncio.create_task(websocket_heartbeat(websocket))
        
        request_handler_task = asyncio.create_task(handle_requests())
        request_processor_task = asyncio.create_task(process_requests(endpoint))
        
        done, pending = await asyncio.wait(
            [request_handler_task, request_processor_task],
            return_when=asyncio.FIRST_COMPLETED,
        )

        for task in done:
            if task.exception():
                logger.error(f"SHTTP: A critical SHTTP task failed: {task.exception()}")
                raise task.exception()

    except websockets.exceptions.ConnectionClosedError as e_ws_closed:
        logger.error(f"SHTTP: WebSocket connection closed during operation: {e_ws_closed}")
        if e_ws_closed.code == 4004:
            logger.error("SHTTP: WebSocket closed due to 4004, will be retried by main loop.")
        raise
    except Exception as e_main_shttp:
        logger.error(f"SHTTP: Main error in pipe_streamable_http: {e_main_shttp}")
        raise
    finally:
        logger.info("SHTTP: pipe_streamable_http is finishing or being cleaned up.")
        if request_queue and request_processor_task and not request_processor_task.done():
            await request_queue.put(None)

        tasks_to_cancel = [http_heartbeat_task, ws_heartbeat_task, request_handler_task, request_processor_task]
        for task in tasks_to_cancel:
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    logger.info(f"SHTTP: Task {task.get_name()} was cancelled successfully.")
                except Exception as e_cancel:
                    logger.error(f"SHTTP: Error during task {task.get_name()} cleanup: {e_cancel}")

async def initialize_session(session, endpoint):
    """Initialize session and get tools list. Returns session ID if available."""
    try:
        logger.info("Sending tools/list request to initialize session")
        
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream"
        }
        data = json.dumps({
            "jsonrpc": "2.0",
            "method": "tools/list",
            "id": 1
        })
        
        logger.debug(f"Sending to endpoint: {endpoint}")
        logger.debug(f"Request payload: {data}")
        
        async with session.post(endpoint, data=data, headers=headers) as response:
            if response.status not in [200, 202]:
                error_text = await response.text()
                logger.error(f"Failed to initialize session: Status {response.status}")
                logger.error(f"Error response: {error_text}")
                return None
                
            session_id = response.headers.get('Mcp-Session-Id')
            if session_id:
                logger.info(f"Received session ID: {session_id}")
            
            logger.info("Session initialization request sent successfully")
            response_text = await response.text()
            logger.debug(f"Initialization response: {response_text}")
            
            try:
                response_data = json.loads(response_text)
                if 'result' in response_data and isinstance(response_data['result'], dict):
                    if 'sessionId' in response_data['result']:
                        session_id = response_data['result']['sessionId']
                        logger.info(f"Using session ID from response body: {session_id}")
            except json.JSONDecodeError:
                pass
                
            return session_id
    except Exception as e:
        logger.error(f"Error initializing session: {e}")
        return None

async def send_heartbeat(session, endpoint, session_id_or_callable=None):
    """Send periodic heartbeat to keep connection alive"""
    try:
        logger.info(f"Started heartbeat task to {endpoint}")
        
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream"
        }
            
        data = json.dumps({
            "jsonrpc": "2.0",
            "method": "ping",
            "params": {}
        })
        
        while True:
            await asyncio.sleep(20) # Standard 20-second interval for heartbeats

            current_session_id = None
            if callable(session_id_or_callable):
                current_session_id = session_id_or_callable()
            elif session_id_or_callable is not None:
                current_session_id = session_id_or_callable

            # Prepare headers for the current heartbeat
            current_headers = headers.copy()
            if current_session_id:
                current_headers["Mcp-Session-Id"] = current_session_id
            else:
                # Ensure Mcp-Session-Id is not sent if no session_id is available
                current_headers.pop("Mcp-Session-Id", None)
                    
            try:
                logger.debug(f"Sending heartbeat to {endpoint} with session_id: {current_session_id}")
                async with session.post(endpoint, data=data, headers=current_headers) as response:
                    if response.status in [200, 202]:
                        logger.debug(f"Heartbeat successful: {response.status}")
                        # Heartbeat should not be authoritative for changing session_id.
                        # Session ID updates should come from the main request/response flow
                        # or initialization. So, we don't update session_id from heartbeat response here.
                    else:
                        response_text = await response.text()
                        logger.warning(f"Heartbeat failed: {response.status} - {response_text}")
                        if response.status == 4004: # Specific error code indicating potential session invalidation
                            logger.error("Server internal error (4004) during heartbeat, likely session terminated.")
                            # Propagate an error that could trigger reconnection or session re-initialization
                            raise websockets.exceptions.ConnectionClosedError(
                                4004, "Server internal error during heartbeat (session may be invalid)"
                            )
            except aiohttp.ClientError as e: # More specific exception handling for network issues
                logger.warning(f"Error sending heartbeat (network issue): {e}")
                # Depending on the error, this might lead to the main connection retry logic
                # if the connection is indeed broken.
            except websockets.exceptions.ConnectionClosedError: # Re-raise if it's a specific close error
                raise
            except Exception as e:
                logger.warning(f"Generic error sending heartbeat: {e}")
                # Avoid crashing the heartbeat loop for non-fatal errors, but log them.
                # If the error implies a closed connection (e.g., from response.status == 4004 logic),
                # it should be re-raised to be caught by the main connection handler.

    except asyncio.CancelledError:
        logger.info(f"Heartbeat task to {endpoint} cancelled")
        raise # Ensure cancellation propagates
    except Exception as e: # Catch-all for the outer loop of send_heartbeat
        logger.error(f"Fatal error in heartbeat task to {endpoint}: {e}")
        raise # Re-raise to ensure the main connection logic can react

async def websocket_heartbeat(websocket):
    """Keep WebSocket connection alive with ping/pong"""
    try:
        while True:
            await asyncio.sleep(20)
            try:
                pong_waiter = await websocket.ping()
                await asyncio.wait_for(pong_waiter, timeout=10)  
                logger.debug("WebSocket ping/pong successful")
            except asyncio.TimeoutError:
                logger.warning("WebSocket pong timeout")
                raise websockets.exceptions.ConnectionClosed(
                    None, None, "Pong timeout"
                )
            except Exception as e:
                logger.warning(f"WebSocket ping failed: {e}")
                raise  
    except asyncio.CancelledError:
        logger.info("WebSocket heartbeat cancelled")
        raise

def load_config(config_file):
    """Load configuration from a YAML file"""
    try:
        with open(config_file, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)
        return config
    except Exception as e:
        logger.error(f"Error loading config file: {e}")
        return None

def signal_handler(sig, frame):
    """Handle interrupt signals"""
    logger.info("Received interrupt signal, shutting down...")
    sys.exit(0)

if __name__ == "__main__":
    signal.signal(signal.SIGINT, signal_handler)
    
    parser = argparse.ArgumentParser(description='MCP pipe for WebSocket and SSE communication')
    parser.add_argument('target', help='MCP script or config file')
    parser.add_argument('--debug', action='store_true', help='Enable debug logging')
    args = parser.parse_args()
    
    # Set debug level
    set_debug_level(args.debug)
    
    target_arg = args.target
    
    if target_arg.endswith('.yaml') or target_arg.endswith('.yml'):
        config = load_config(target_arg)
        if not config:
            logger.error("Failed to load configuration file")
            sys.exit(1)
        
        endpoint_url = config.get('mcp_endpoint')
        mode = config.get('mode', 'stdio')
        
        if not endpoint_url:
            logger.error("MCP_ENDPOINT must be defined in the config file (mcp_endpoint)")
            sys.exit(1)

        if mode == 'sse':
            sse_url = config.get('sse_url')
            if not sse_url:
                logger.error("sse_url is required in config file for SSE mode")
                sys.exit(1)
            target = sse_url
        elif mode == 'stdio':
            script_path = config.get('script_path')
            if not script_path:
                logger.error("script_path is required in config file for stdio mode")
                sys.exit(1)
            target = script_path
        elif mode == 'streamable_http':
            streamable_url = config.get('streamable_url')
            if not streamable_url:
                logger.error("streamable_url is required in config file for Streamable HTTP mode")
                sys.exit(1)
            target = streamable_url
        else:
            logger.error(f"Unsupported mode '{mode}' in config file. Supported modes are 'stdio', 'sse', and 'streamable_http'.")
            sys.exit(1)
    else:
        endpoint_url = os.environ.get('MCP_ENDPOINT')
        if not endpoint_url:
            logger.error("Please set the `MCP_ENDPOINT` environment variable or use a config file")
            sys.exit(1)
        target = target_arg
        mode = 'stdio'
    
    logger.info(f"Using mode: {mode}")
    logger.info(f"MCP endpoint: {endpoint_url}")
    logger.info(f"Target: {target}")
    
    try:
        asyncio.run(connect_with_retry(endpoint_url, target, mode))
    except KeyboardInterrupt:
        logger.info("Program interrupted by user")
    except Exception as e:
        logger.error(f"Program execution error: {e}")