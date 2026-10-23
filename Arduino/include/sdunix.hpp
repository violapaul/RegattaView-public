
#include "sdcard.hpp"
#include "utils.hpp"

namespace sdunix {

// Define a buffer size for reading and writing files
#define BUFFER_SIZE 64

// Define a global variable to store the current working directory
String cwd = "/";

// A function to print the files and directories in the current working directory
// uses the sdcard.hpp library
void ls() {
    std::vector<sdcard::FileSize> files = sdcard::listFiles(SD, cwd.c_str(), "*");
    for (auto file : files) {
        LOG_INFO("%9d ", file.size);
        LOG_INFO(file.filename.c_str());
    }
}

// A function to print the contents of a file to the serial monitor
// uses the sdcard.hpp library
void cat(String filename, int maxBytes) {
    // Check if the filename is valid
    if (filename.length() > 0) {
        // Append the current working directory to the filename
        filename = cwd + filename;
        // Open the file for reading
        sdcard::printFile(SD, filename.c_str());
    }
}

// A function to change the current working directory
void cd(String dirname) {
    // Check if the dirname is valid
    if (dirname.length() > 0) {
        // Check if the dirname is the root directory
        if (dirname == "/" || dirname == "") {
            // Set the current working directory to the root directory
            cwd = "/";
            // Print the current working directory
            LOG_INFO("Changed directory to ");
            LOG_INFO(cwd);
        } else {
            // strip either a leading or trailing slash
            if (dirname[0] == '/') {
                dirname = dirname.substring(1);
            }
            if (dirname[dirname.length() - 1] == '/') {
                dirname = dirname.substring(0, dirname.length() - 1);
            }
            // Append the current working directory to the dirname
            dirname = cwd + '/' + dirname;
        }
    }
}

// A function to remove a file or an empty directory
void rm(String filename) {
    // Check if the filename is valid
    if (filename.length() > 0) {
        // Append the current working directory to the filename
        filename = cwd + filename;
        sdcard::deleteFile(SD, filename.c_str());
    } else {
        // Print an error message
        LOG_INFO("Error: missing file or directory name");
    }
}

// std::vector<String> split()

// A function to parse and execute a command from the serial monitor
void execute(String command) {
    // Check if the command is valid
    if (command.length() > 0) {
        LOG_INFO("command: %s\n", command.c_str());
        auto tokens = utils::split(command, " ");
        LOG_INFO("tokens: %d\n", tokens.size());
        if (tokens.size() > 0) {
            String cmd = tokens[0];
            String arg = tokens.size() > 1 ? tokens[1] : "";
            LOG_INFO(cmd.c_str());
            LOG_INFO("cmd: |%s|\n", cmd.c_str());
            LOG_INFO(arg.c_str());
            LOG_INFO("arg: |%s|\n", arg.c_str());
            // Check the command name and call the corresponding function
            // LOG_INFO("cmd: %s, arg: %s\n", cmd.c_str(), arg.c_str());
            if (cmd == "ls") {
                ls();
            } else if (cmd == "cat") {
                cat(arg, 4000);
            } else if (cmd == "cd") {
                cd(arg);
            } else if (cmd == "rm") {
                rm(arg);
            } else if (cmd == "rmtxt") {
                sdcard::deleteMatching(SD, "/", ".txt");
            } else if (cmd == "rmm" && arg.length() > 0) {
                sdcard::deleteMatching(SD, "/", arg.c_str());
            } else {
                // Print an error message
                LOG_INFO("Error: unknown command");
            }
        }
    } else {
        LOG_INFO("Error: missing command");
    }
}



// The setup function runs once when the board is powered on or reset
void setup() {}

char buffer[256];
// The loop function runs over and over again forever
void loop() {
    // Print a prompt
    LOG_INFO("%s> ", cwd.c_str());
    // Read a command from the serial monitor
    int readLen;
    bool status = utils::readCommandInteractive(buffer, sizeof(buffer), readLen);
    // Execute the command
    execute(String(buffer));
}
}  // namespace sdunix