"""
Streamlit GUI interface for the MCP Client.
Run with: streamlit run gui_app.py
"""

from __future__ import annotations
import asyncio
import logging
from typing import Any

import streamlit as st
import nest_asyncio

# Apply nest_asyncio to allow nested event loops (required for Streamlit + asyncio)
nest_asyncio.apply()

from config import (
    GAINS_API_TOKEN,
    GAINS_API_URL,
    GEMINI_MODEL,
    GOOGLE_API_KEY,
    LLM,
    MCP_SERVER_SCRIPT,
)
from mcp_client import MCPClient

logger = logging.getLogger(__name__)

# --- Configuration & Initialization ---
st.set_page_config(
    page_title="E-Commerce Shopping Assistant",
    page_icon="🛍️",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Initialize session state variables
if "messages" not in st.session_state:
    st.session_state.messages = []
    
if "client" not in st.session_state:
    st.session_state.client = None
    
if "api" not in st.session_state:
    st.session_state.api = None

# Set up the MCP Client if not already done
@st.cache_resource
def get_or_create_eventloop():
    try:
        return asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        return loop

def _create_llm_api():
    """Factory: return the LLM api object selected by LLM env var."""
    if LLM == "gemini":
        if not GOOGLE_API_KEY:
            st.error("GOOGLE_API_KEY is not set. Please set it in your .env file.")
            st.stop()
        from gemini_api import GeminiAPI
        return GeminiAPI(api_key=GOOGLE_API_KEY, model=GEMINI_MODEL)
    else:  # default: gains
        if not GAINS_API_TOKEN:
            st.error("GAINS_API_TOKEN is not set. Please set it in your .env file.")
            st.stop()
        from gains_api import GainsAPI
        return GainsAPI(base_url=GAINS_API_URL, token=GAINS_API_TOKEN)


async def init_client():
    api = _create_llm_api()
    client = MCPClient(api)
    
    try:
        await client.connect(MCP_SERVER_SCRIPT)
        st.session_state.api = api
        st.session_state.client = client
    except Exception as exc:
        st.error(f"Failed to connect to MCP server: {exc}")
        st.stop()

# --- Sidebar UI ---
with st.sidebar:
    llm_label = "Google Gemini" if LLM == "gemini" else "Gains AI"
    st.title("🛍️ Shopping Assistant")
    st.markdown(f"Powered by {llm_label} + MCP")
    
    if st.session_state.client is None:
        with st.spinner("Connecting to MCP Server..."):
            loop = get_or_create_eventloop()
            loop.run_until_complete(init_client())
    
    if st.session_state.client is not None:
        st.success("✅ Connected to Server")
        
        # Display Tools
        st.subheader("Available Tools")
        tools = st.session_state.client.get_active_tools()
        for t in tools:
            with st.expander(f"🔧 {t['name']}"):
                st.write(t["description"])
                
        # Session Management
        st.divider()
        if st.button("Clear Conversation"):
            st.session_state.messages = []
            st.session_state.client.session_id = None
            st.rerun()

# --- Main Chat UI ---
st.title("Chat")

# Display chat history
for message in st.session_state.messages:
    if message["role"] == "user":
        with st.chat_message("user", avatar="👤"):
            st.markdown(message["content"], unsafe_allow_html=True)
    elif message["role"] == "assistant":
        with st.chat_message("assistant", avatar="🤖"):
            # If there are tool calls associated with this message, display them in an expander
            if "tool_log" in message and message["tool_log"]:
                with st.expander("🛠️ View Tool Executions", expanded=False):
                    for idx, log in enumerate(message["tool_log"]):
                        st.markdown(f"**Tool:** `{log['tool']}`")
                        st.markdown(f"**Args:** `{log['args']}`")
                        st.text_area("Result:", value=log['result'], height=100, disabled=True, key=f"res_{message['id']}_{idx}")
                        st.divider()
            
            st.markdown(message["content"], unsafe_allow_html=True)

# Chat input
if prompt := st.chat_input("Ask me about products, search, or manage your cart..."):
    # Add user message to state and display
    st.session_state.messages.append({"role": "user", "content": prompt, "id": len(st.session_state.messages)})
    with st.chat_message("user", avatar="👤"):
        st.markdown(prompt, unsafe_allow_html=True)

    # Generate assistant response
    with st.chat_message("assistant", avatar="🤖"):
        message_placeholder = st.empty()
        status_container = st.container()
        
        with status_container:
            status = st.status("Thinking...", expanded=True)
            
            def _on_status(msg: str) -> None:
                status.update(label=msg)
                
            try:
                loop = get_or_create_eventloop()
                # Run the chat method and capture the reply and tool logs
                reply, tool_log = loop.run_until_complete(
                    st.session_state.client.chat(prompt, on_status=_on_status)
                )
                
                status.update(label="Response generated!", state="complete", expanded=False)
                
                # Show tool logs if any
                if tool_log:
                    for log in tool_log:
                        status.write(f"Executed `{log['tool']}` with args `{log['args']}`")
                
            except Exception as e:
                status.update(label=f"Error: {str(e)}", state="error")
                logger.exception("Chat error")
                reply = "Sorry, I encountered an error while processing your request."
                tool_log = []

        # Display final response
        message_placeholder.markdown(reply, unsafe_allow_html=True)
        
        # Add assistant message to state
        st.session_state.messages.append({
            "role": "assistant", 
            "content": reply, 
            "tool_log": tool_log,
            "id": len(st.session_state.messages)
        })
