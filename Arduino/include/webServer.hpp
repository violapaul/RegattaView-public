#pragma once

#include "board.hpp"
#include "display.hpp"

#include <WiFi.h>
#include <WiFiAP.h>
#include <WiFiClient.h>
#include <WiFiServer.h>
#include <map>
#include <vector>

#include "sdcard.hpp"

#define WS_FAIL "__FAIL__"

namespace webServer {
WiFiServer server(80);
bool networkSetup = false;

bool wifiCreateAccessPoint(const char* ssid, const char* password, String hostname = "boatBrain") {
    if (networkSetup) {
        LOG_ERROR("Network already setup.");
        return false;
    }
    if (!password || strlen(password) < 16 || strlen(password) > 63) {
        LOG_ERROR("Provision a device WiFi password (16-63 characters) before enabling the AP.");
        return false;
    }
    LOG_INFO("Creating WiFi Access Point network:", ssid);
    WiFi.mode(WIFI_AP);
    WiFi.setHostname(hostname.c_str()); //define hostname
    if (!WiFi.softAP(ssid, password)) {
        LOG_ERROR("WiFi AP creation failed.");
        return false;
    }
    LOGF_INFO("WiFi AP created. IP address: %s", WiFi.softAPIP().toString().c_str());
    networkSetup = true;
    return true;
}


bool wifiJoinNetwork(const char* ssid, const char* password, int maxTries = 10, String hostname = "BoatBrain") {
    if (networkSetup) {
        LOG_ERROR("Network already setup.");
        return false;
    }
    if (!ssid || !ssid[0] || !password || !password[0]) return false;
    LOGF_INFO("Joining WiFi network: %s", ssid);
    WiFi.mode(WIFI_STA);
    WiFi.config(INADDR_NONE, INADDR_NONE, INADDR_NONE, INADDR_NONE);
    WiFi.setHostname(hostname.c_str()); //define hostname
    int tries = 0;
    if (WiFi.begin(ssid, password)) {
        while (tries++ < maxTries && !WiFi.isConnected()) {
            delay(200);
            LOG_INFO("Waiting for connection, try:", tries, WiFi.isConnected());
        }
        if (WiFi.isConnected()) {
            LOG_INFO("WiFi AP joined. IP address:", WiFi.localIP().toString());
            networkSetup = true;
            return true;
        }
    }
    LOG_ERROR("WiFi AP creation failed.");
    WiFi.disconnect();
    return false;
}


bool serverSetup() {
    if (!networkSetup) {
        LOG_ERROR("Network not setup.");
        return false;
    }
    server.begin();
    return true;
}

// create a type shorthand for a vector of Strings
typedef std::vector<String> Path;

// Handles a web request, usually in a thread
typedef void (*Handler)(WiFiClient* client, Path path, String request);

// Create a struct that passes the arguments to the thread
class TaskArgs {
   public:
    Handler handler;      // What handle to call
    WiFiClient* client;   // The client to respond to
    Path path;            // The full path of the request  
    String request;       // The full request
    // Constructor
    TaskArgs(Handler handler, WiFiClient* client, Path path, String request) : handler(handler), client(client), path(path), request(request) {}

    // // Destructor
    // ~HandlerArgs() {
    //     // For some reason I can't delete the client
    //     // delete client;
    //     LOG_INFO("HandlerArgs destructor");
    // }
};


// create map from path name to handler
std::map<String, Handler> handlers;

// Create a vector of handlers that can be called based on the path
// The handler will return a string that will be sent back to the client
// The handler will be passed the path and the request
bool addHandler(String head, Handler handler) {
    LOG_INFO("Adding handler:", head);
    handlers[head] = handler;
    return true;
}

std::vector<String> extractPath(String request) {
    // Example is GET /favicon.ico HTTP/1.1
    // Find a line that starts with GET
    int start = request.indexOf("GET ");
    // extract the portion up to the protocol
    int end = request.indexOf(" HTTP/");
    // extract the path
    String path = request.substring(start + 4, end);
    // remove the leading slash, if any
    if (path.startsWith("/")) {
        path = path.substring(1);
    } else {
        LOG_INFO("WARNING: Path does not start with /");
    }
    // if empty, return the root path
    if (path.length() == 0) {
        std::vector<String> result;
        result.push_back("");
        return result;
    }
    return utils::split(path, "/");
}

void header200(WiFiClient* client, String contentType) {
    client->write("HTTP/1.1 200 OK\n");
    String message = "Content-type:" + contentType + "\n";
    client->write(message.c_str());
    client->write("\n");
}

// This is the thread that gets kicked off, it calls the handler and then cleans up.  (The handlers know nothing about threads.)
void taskExecutor(void* pvParameters) {
    TaskArgs* args = static_cast<TaskArgs*>(pvParameters);
    Client *client = args->client;
    args->handler(args->client, args->path, args->request);
    LOG_INFO("taskExecutor: handler flushing");
    client->flush();
    LOG_INFO("taskExecutor: handler stopping");
    client->stop();
    LOG_INFO("taskExecutor: deleting client");
    delete client;
    delete args;
    vTaskDelete(NULL);
}

// handle the path request, finds the handler and calls it in a thread.
void handlePath(WiFiClient* client, Path path, String request) {
    // find the handler
    String head = path[0];
    auto handler = handlers.find(head);
    if (handler != handlers.end()) {
        // Create the hanlderArgs on the heap
        TaskArgs *args = new TaskArgs(handler->second, client, path, request);
        LOG_INFO("Handler found: ", head);
        // call the handler in a thread
        BaseType_t res = xTaskCreatePinnedToCore(
            taskExecutor, /* Function to implement the task */
                                head.c_str(),    /* Name of the task */
                                1024 + 10 * 4096,       /* Stack size in words */
                                args,            /* Task input parameter */
                                1,               /* Priority of the task */
                                NULL,            /* Task handle. */
                                0);              /* Core where the task should run */
        LOG_INFO("handlePath: thread created ");
        LOG_INFO(res);

    } else {
        LOG_INFO("Handler not found: ", head);
        // return a 404
        client->write("HTTP/1.1 404 Not Found\n");
        client->write("Content-type:text/html\n");
        auto message = "<p>The requested URL " + utils::join(path, "/") + " was not found on this server.</p>\n";
        client->write(message.c_str());
        client->write("\n");
    }
}

// Waits for a request, creates a client, and then collects the header, and extracts the path.
void handleRequest() {
    WiFiClient possibleClient = server.available();  // listen for incoming clients

    if (possibleClient) {
        LOG_INFO("Handle Request: New Client.");
        // Make a client in the heap, so that it can be passed to the thread in the handler
        WiFiClient* client = new WiFiClient(possibleClient);
        String request = "";
        String currentLine = "";          // make a String to hold incoming data from the client
        while (client->connected()) {     // loop while the client's connected
            if (client->available()) {    // if there's bytes to read from the client,
                char c = client->read();  // read a byte, then
                if (c == '\n')            // if the byte is a newline character
                {
                    // if the current line is blank, you got two newline characters in a row.
                    // that's the end of the client HTTP request, so send a response:
                    if (currentLine.length() == 0) {
                        // extract the path
                        Path path = extractPath(request);
                        LOG_INFO("Web Request Path:  ", path[0], utils::join(path, "___"));
                        handlePath(client, path, request);
                        break;
                    }
                    request = request + currentLine;
                    currentLine = "";
                } else if (c != '\r') {  // if you got anything else but a carriage return character,
                    currentLine += c;    // add it to the end of the currentLine
                }
            }
        }
    }
}

}  // namespace webServer
