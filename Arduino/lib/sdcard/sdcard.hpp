
#pragma once

#include "board.hpp"

#include "FS.h"
#include "SPI.h"
#include "SD.h"

#include <vector>
#include <tuple>

#include "utils.hpp"

#if defined(CORES3)
    #define SDCARD SD
#else
#include "SD_MMC.h"
    #define SDCARD SD_MMC
#endif

namespace sdcard
{
    // tiny struct that stores a filename and its size
    struct FileSize
    {
        String filename;
        size_t size;
    };

    void appendFile(fs::FS &fs, const char *path, const char *message);
    void appendFile(const char *path, const char *message);

    void createDir(fs::FS &fs, const char *path);
    void createDir(const char *path);

    // void createTestFiles(int numFiles, int line_count);
    void deleteFile(fs::FS &fs, const char *path);
    void deleteFile(const char *path);

    void exercise();
    uint8_t getCardType(bool printInfo);

    std::vector<FileSize> listFiles(fs::FS &fs, const char *dirname, const char *pattern);
    std::vector<FileSize> listFiles(const char *dirname, const char *pattern);

    void printFile(fs::FS &fs, const char *path);
    void printFile(const char *path);

    void removeDir(fs::FS &fs, const char *path);
    void removeDir(const char *path);

    void renameFile(fs::FS &fs, const char *path1, const char *path2);
    void renameFile(const char *path1, const char *path2);

    bool setup(int countTries);
    bool hasCard();
    bool isHealthy();

    void readTest(fs::FS &fs, const char *path);
    void readTest(const char *path);

    void writeBig(fs::FS &fs, const char *path, int line_count);
    void writeBig(const char *path, int line_count);

    void writeFile(fs::FS &fs, const char *path, const char *message);
    void writeFile(const char *path, const char *message);

    void deleteMatching(fs::FS &fs, const char *dirname, const char *pattern);
    void deleteMatching(const char *dirname, const char *pattern);

    File openFile(const char *filename, const char* mode);
}