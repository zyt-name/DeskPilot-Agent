// Run this file inside the matching Xshell tab (Tools -> Script -> Run).
// It starts the xshell-mcp bridge for session "default".
var XSHELL_MCP_CONFIG = "D:\\xm\\agent\\config\\xshell-mcp.json";
var SESSION_ID = "default";
eval(new ActiveXObject("Scripting.FileSystemObject").OpenTextFile("D:\\xm\\agent\\vendor\\xshell-mcp\\xshell\\bridge.js", 1, false, -2).ReadAll());
