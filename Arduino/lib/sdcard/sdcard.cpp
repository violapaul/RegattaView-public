
#include "sdcard.hpp"
#include "display.hpp"

#include "utils.hpp"

#define ONE_BIT_MODE false

// name a namespace
namespace sdcard
{


    void appendFile(const char *path, const char *message) { appendFile(SDCARD, path, message); }
    void createDir(const char *path) { createDir(SDCARD, path); }
    void deleteFile(const char *path) { deleteFile(SDCARD, path); }

    std::vector<FileSize> listFiles(const char *dirname, const char *pattern) { 
        return listFiles(SDCARD, dirname, pattern); 
    }

    void printFile(const char *path) { printFile(SDCARD, path); }
    void removeDir(const char *path) { removeDir(SDCARD, path); }
    void renameFile(const char *path1, const char *path2) { renameFile(SDCARD, path1, path2); }
    void readTest(const char *path) { readTest(SDCARD, path); }
    void writeBig(const char *path, int line_count) { writeBig(SDCARD, path, line_count); }
    void writeFile(const char *path, const char *message) { writeFile(SDCARD, path, message); }
    void deleteMatching(const char *dirname, const char *pattern) { deleteMatching(SDCARD, dirname, pattern); }

    File openFile(const char* filename, const char* mode) {
        return SDCARD.open(filename, mode);      
    }

    // function that returns an list of filenames that match a pattern
    std::vector<FileSize> listFiles(fs::FS &fs, const char *dirname, const char *pattern)
    {
        std::vector<FileSize> files;
        LOGF_INFO("Listing directory: %s", dirname);

        File root = fs.open(dirname);
        if (!root)
        {
            LOG_INFO("Failed to open directory");
            return files;
        }
        if (!root.isDirectory())
        {
            LOG_INFO("Not a directory");
            return files;
        }

        while (true) {
            // Read the next file or directory
            File entry = root.openNextFile();
            // Check if there is no more entry
            if (!entry) {
                // Break the loop
                break;
            }
            files.push_back({entry.name(), entry.size()});
            entry.close();
        }
        return files;
    }



    void createDir(fs::FS &fs, const char *path)
    {
        LOGF_INFO("Creating Dir: %s", path);
        if (fs.mkdir(path))
        {
            LOG_INFO("Dir created");
        }
        else
        {
            LOG_INFO("mkdir failed");
        }
    }

    void removeDir(fs::FS &fs, const char *path)
    {
        LOGF_INFO("Removing Dir: %s", path);
        if (fs.rmdir(path))
        {
            LOG_INFO("Dir removed");
        }
        else
        {
            LOG_INFO("rmdir failed");
        }
    }

    void printFile(fs::FS &fs, const char *path)
    {
        LOGF_INFO("Reading file: %s", path);

        File file = fs.open(path);
        if (!file)
        {
            LOG_INFO("Failed to open file for reading");
            return;
        }

        LOG_INFO("Read from file: ");
        while (file.available())
        {
            Serial.write(file.read());
        }
        file.close();
    }

    void writeFile(fs::FS &fs, const char *path, const char *message)
    {
        LOGF_INFO("Writing file: %s", path);

        File file = fs.open(path, FILE_WRITE);
        if (!file)
        {
            LOG_INFO("Failed to open file for writing");
            return;
        }
        if (file.print(message))
        {
            LOG_INFO("File written");
        }
        else
        {
            LOG_INFO("Write failed");
        }
        file.close();
    }

    // delete all matching files in directory
    void deleteMatching(fs::FS &fs, const char *dirname, const char *pattern)
    {
        auto files = listFiles(fs, dirname, pattern);
        LOGF_INFO("Deleting %d files", files.size());
        for (auto fileSize : files)
        {
            deleteFile(fs, fileSize.filename.c_str());
        }
    }

    // write a big file and time how long it takes
    void writeBig(fs::FS &fs, const char *path, int byteCount)
    {
        LOGF_INFO("Writing file: %s", path);

        File file = fs.open(path, FILE_WRITE);
        if (!file)
        {
            LOG_INFO("Failed to open file for writing");
            return;
        }
        // start the timer
        uint32_t start = millis();
        // create a line with a short message
        auto line = String("Hello World! Now is the time for all good men to come to the aid of their county.\n");
        auto block = String("");
        // expand line by making 10 copies
        for (int i = 0; i < 10; i++)
        {
            block = block + line;
        }
        int lineCount = byteCount / block.length();
        for (int i = 0; i < lineCount; i++)
        {
            if (!file.print(block))
            {
                LOG_INFO("Write failed");
                break;
            }
        }
        uint32_t end = millis();
        int total_file_length = file.position();
        LOGF_INFO("File written of length %d in %d ms", total_file_length, end - start);
        // MB per second
        LOGF_INFO("Write speed: %0.3f MB/s", float(total_file_length) / float(end - start) * 1000.0 / float(1024 * 1024));
        file.close();
    }

    void appendFile(fs::FS &fs, const char *path, const char *message)
    {
        LOGF_INFO("Appending to file: %s", path);

        File file = fs.open(path, FILE_APPEND);
        if (!file)
        {
            LOG_INFO("Failed to open file for appending");
            return;
        }
        if (file.print(message))
        {
            LOG_INFO("Message appended");
        }
        else
        {
            LOG_INFO("Append failed");
        }
        file.close();
    }

    void renameFile(fs::FS &fs, const char *path1, const char *path2)
    {
        LOGF_INFO("Renaming file %s to %s", path1, path2);
        if (fs.rename(path1, path2))
        {
            LOG_INFO("File renamed");
        }
        else
        {
            LOG_INFO("Rename failed");
        }
    }

    void deleteFile(fs::FS &fs, const char *path)
    {
        LOGF_INFO("Deleting file: %s", path);
        if (fs.remove(path))
        {
            LOG_INFO("File deleted");
        }
        else
        {
            LOG_INFO("Delete failed");
        }
    }

    void readTest(fs::FS &fs, const char *path)
    {
        File file = fs.open(path);
        static uint8_t buf[512];
        size_t len = 0;
        uint32_t start = millis();
        uint32_t end = start;
        if (file)
        {
            len = file.size();
            size_t flen = len;
            start = millis();
            while (len)
            {
                size_t toRead = len;
                if (toRead > 512)
                {
                    toRead = 512;
                }
                file.read(buf, toRead);
                len -= toRead;
            }
            end = millis() - start;
            LOGF_INFO("%u bytes read for %u ms", flen, end);
            file.close();
        }
        else
        {
            LOG_INFO("Failed to open file for reading");
        }
    }

    // sets up the connection to the SD card.  Use this instead of the raw SDCARD.begin()
    // because that sometimes fails on startup.
    bool setup(int countTries)
    {
        int tries = 0;

#if defined(ESP32_S3_DEVKITC_1)
    LOG_INFO("Using ESP32-S3-DevKitC-1");
    SDCARD.setPins(41, 42, 40, 39, 1, 2);
    LOG_INFO("Pins set!");
    #define BEGIN SDCARD.begin()
#elif defined(CORES3)
    #define SD_SPI_SCK_PIN  36
    #define SD_SPI_MISO_PIN 35
    #define SD_SPI_MOSI_PIN 37
    #define SD_SPI_CS_PIN   4
    LOG_INFO("Using CoreS3");
    SPI.begin(SD_SPI_SCK_PIN, SD_SPI_MISO_PIN, SD_SPI_MOSI_PIN, SD_SPI_CS_PIN);
    #define BEGIN SDCARD.begin(SD_SPI_CS_PIN, SPI, 25000000)
#else
    #define BEGIN SDCARD.begin()
#endif

        while (tries < countTries)
        {
            LOG_INFO("Mounting SD card...");
            if (! BEGIN) // !SDCARD.begin("/sdcard", true))
            {
                LOG_INFO("Card Mount attempt number " + String(tries));
                tries++;
                delay(500);
            }
            else
            {
                LOGF_INFO("Success on attempt %d", tries);
                getCardType(true);
                File file = SDCARD.open("/dummy.txt", FILE_WRITE);
                if (file) {
                    file.close();
                    LOG_INFO("Dummy file created");
                    return true;
                }
            }
        }
        return false;
    }

    bool isHealthy()
    {
        // check if dummy file exists
        return SDCARD.exists("/dummy.txt");
    }

    bool hasCard()
    {
        return SDCARD.cardType() != CARD_NONE;
    }

    // function that returns the card type, and optinally prints the type
    uint8_t getCardType(bool printInfo)
    {
        uint8_t cardType = SDCARD.cardType();
        if (printInfo)
        {
            if (cardType == CARD_NONE)
            {
                LOG_INFO("No SD card attached");
            }
            else if (cardType == CARD_MMC)
            {
                LOG_INFO("MMC card");
            }
            else if (cardType == CARD_SD)
            {
                LOG_INFO("SDSC card");
            }
            else if (cardType == CARD_SDHC)
            {
                LOG_INFO("SDHC card");
            }
            else
            {
                LOG_INFO("Unknown card type");
            }
            // show total and free space
            long cardSize = SDCARD.cardSize() / (1024 * 1024);
            long totalBytes = SDCARD.totalBytes() / (1024 * 1024);
            long usedBytes = SDCARD.usedBytes() / (1024 * 1024);
            LOGF_INFO("SD Card Size: %lluMB", cardSize);
            LOGF_INFO("SD Card Total space: %lluMB", totalBytes);
            LOGF_INFO("SD Card Used space: %lluMB", usedBytes);
        }
        return cardType;
    }

    // create a set of test files and time write and read
    void createTestFiles(int numFiles, int byteCount)
    {
        for (int i = 0; i < numFiles; i++)
        {
            // generate a positive random number
            uint32_t r = random(1, 1000000);
            // create a file name with this number
            char filename[32];
            // 6 digits zero padded
            sprintf(filename, "/file_%06d.txt", r);
            LOGF_INFO("Filename: %s", filename);
            // a big file with this name
            writeBig(SD, filename, byteCount);
            // and time how long it takes to read it
            readTest(SD, filename);
        }
    }

    // exercise the SD card functions
    void exercise()
    {
        // list all files in the card with the .txt extension
        auto files = listFiles("/", ".txt");
        LOGF_INFO("Found %d files", files.size());
        for (auto fileSize : files)
        {
            LOG_INFO(fileSize.filename.c_str());
        }

        // create a directory
        createDir("/mydir");
        // create a file in the directory
        writeFile("/mydir/hello.txt", "Hello ");
        // append more text to the file
        appendFile("/mydir/hello.txt", "World!\n");
        // read the file
        printFile("/mydir/hello.txt");
        // rename the file
        renameFile("/mydir/hello.txt", "/mydir/hello_world.txt");
        // read the file
        printFile("/mydir/hello_world.txt");
        // delete the file
        deleteFile("/mydir/hello_world.txt");
        // delete the directory
        removeDir("/mydir");
    }
}