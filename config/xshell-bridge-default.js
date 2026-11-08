// Run this file inside the matching Xshell tab (Tools -> Script -> Run).
// It starts the xshell-mcp bridge for session "default".
// Set REPO_ROOT to the absolute path of this repository before running.
var REPO_ROOT = "D:\\path\\to\\DeskPilot-Agent";
var XSHELL_MCP_CONFIG = REPO_ROOT + "\\config\\xshell-mcp.json";
var SESSION_ID = "default";
eval(new ActiveXObject("Scripting.FileSystemObject").OpenTextFile(REPO_ROOT + "\\vendor\\xshell-mcp\\xshell\\bridge.js", 1, false, -2).ReadAll());
