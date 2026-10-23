//
//  LightLog.swift
//  BoatStart
//
//  Created by Paul Viola on 1/2/24.
//

import Foundation
import os.log

// setup logging levels as enums and integers so that they can be compared
enum LoggingLevel: Int {
    case debug = 0
    case info
    case warn
    case error
    case critical

    var prefix: String {
        switch self {
        case .debug: return "D"
        case .info: return "I"
        case .warn: return "W"
        case .error: return "E"
        case .critical: return "C"
        }
    }
}

var LOGGING_LEVEL = LoggingLevel.info

// OSLog logger for device console visibility
private let logger = Logger(subsystem: "foobar.BoatData", category: "app")

// Persistent file logger
class FileLog {
    static let shared = FileLog()

    private let maxFileSize = 500_000  // 500KB max per file
    private let maxFiles = 3           // Keep 3 log files
    private let queue = DispatchQueue(label: "fileLog", qos: .utility)
    private var fileHandle: FileHandle?
    private var currentFileSize = 0

    private let dateFormatter: DateFormatter = {
        let f = DateFormatter()
        f.dateFormat = "HH:mm:ss.SSS"
        return f
    }()

    private var logDirectory: URL? {
        FileManager.default.urls(for: .documentDirectory, in: .userDomainMask).first?.appendingPathComponent("logs")
    }

    init() {
        setupLogDirectory()
        openCurrentLogFile()
    }

    private func setupLogDirectory() {
        guard let dir = logDirectory else { return }
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    }

    private func currentLogPath() -> URL? {
        logDirectory?.appendingPathComponent("app.log")
    }

    private func openCurrentLogFile() {
        guard let path = currentLogPath() else { return }

        if !FileManager.default.fileExists(atPath: path.path) {
            FileManager.default.createFile(atPath: path.path, contents: nil)
        }

        fileHandle = try? FileHandle(forWritingTo: path)
        fileHandle?.seekToEndOfFile()
        currentFileSize = Int(fileHandle?.offsetInFile ?? 0)
    }

    func write(_ level: LoggingLevel, _ message: String) {
        queue.async { [weak self] in
            guard let self = self, let handle = self.fileHandle else { return }

            let timestamp = self.dateFormatter.string(from: Date())
            let line = "[\(timestamp)] \(level.prefix): \(message)\n"

            if let data = line.data(using: .utf8) {
                handle.write(data)
                self.currentFileSize += data.count

                // Rotate if needed
                if self.currentFileSize > self.maxFileSize {
                    self.rotateLogFiles()
                }
            }
        }
    }

    private func rotateLogFiles() {
        fileHandle?.closeFile()
        fileHandle = nil

        guard let dir = logDirectory else { return }

        // Delete oldest, shift others
        let log2 = dir.appendingPathComponent("app.2.log")
        let log1 = dir.appendingPathComponent("app.1.log")
        let log0 = dir.appendingPathComponent("app.log")

        try? FileManager.default.removeItem(at: log2)
        try? FileManager.default.moveItem(at: log1, to: log2)
        try? FileManager.default.moveItem(at: log0, to: log1)

        // Create new current log
        FileManager.default.createFile(atPath: log0.path, contents: nil)
        fileHandle = try? FileHandle(forWritingTo: log0)
        currentFileSize = 0
    }

    func readLogs(maxLines: Int = 500) -> String {
        guard let dir = logDirectory else { return "" }

        var allLogs = ""

        // Read in reverse order (oldest first)
        for name in ["app.2.log", "app.1.log", "app.log"] {
            let path = dir.appendingPathComponent(name)
            if let content = try? String(contentsOf: path, encoding: .utf8) {
                allLogs += content
            }
        }

        // Return last N lines
        let lines = allLogs.components(separatedBy: "\n")
        let lastLines = lines.suffix(maxLines)
        return lastLines.joined(separator: "\n")
    }

    func clearLogs() {
        queue.async { [weak self] in
            self?.fileHandle?.closeFile()
            self?.fileHandle = nil

            guard let dir = self?.logDirectory else { return }

            for name in ["app.log", "app.1.log", "app.2.log"] {
                try? FileManager.default.removeItem(at: dir.appendingPathComponent(name))
            }

            self?.openCurrentLogFile()
        }
    }

    func flush() {
        queue.sync {
            fileHandle?.synchronizeFile()
        }
    }
}

func logDebug(_ message: String, file: String = #file, function: String = #function) {
    if LOGGING_LEVEL.rawValue <= LoggingLevel.debug.rawValue {
        logger.debug("\(message, privacy: .public)")
        FileLog.shared.write(.debug, message)
    }
}

func logInfo(_ message: String, file: String = #file, function: String = #function) {
    if LOGGING_LEVEL.rawValue <= LoggingLevel.info.rawValue {
        logger.info("\(message, privacy: .public)")
        FileLog.shared.write(.info, message)
    }
}

func logWarn(_ message: String, file: String = #file, function: String = #function) {
    if LOGGING_LEVEL.rawValue <= LoggingLevel.warn.rawValue {
        logger.warning("\(message, privacy: .public)")
        FileLog.shared.write(.warn, message)
    }
}

func logError(_ message: String, file: String = #file, function: String = #function) {
    if LOGGING_LEVEL.rawValue <= LoggingLevel.error.rawValue {
        logger.error("\(message, privacy: .public)")
        FileLog.shared.write(.error, message)
    }
}

func logCritical(_ message: String, file: String = #file, function: String = #function) {
    if LOGGING_LEVEL.rawValue <= LoggingLevel.critical.rawValue {
        logger.critical("\(message, privacy: .public)")
        FileLog.shared.write(.critical, message)
    }
}

func setLoggingLevel(_ level: LoggingLevel) {
    LOGGING_LEVEL = level
}
