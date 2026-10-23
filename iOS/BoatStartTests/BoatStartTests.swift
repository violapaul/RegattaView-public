//
//  BoatStartTests.swift
//  BoatStartTests
//
//  Created by Paul Viola on 11/16/22.
//

import XCTest
@testable import BoatStart

final class BoatStartTests: XCTestCase {
    
    override func setUpWithError() throws {
        // Put setup code here. This method is called before the invocation of each test method in the class.
    }
    
    override func tearDownWithError() throws {
        // Put teardown code here. This method is called after the invocation of each test method in the class.
    }
    
    func testExample() throws {
        // This is an example of a functional test case.
        // Use XCTAssert and related functions to verify your tests produce the correct results.
        // Any test you write for XCTest can be annotated as throws and async.
        // Mark your test throws to produce an unexpected failure when your test encounters an uncaught error.
        // Mark your test async to allow awaiting for asynchronous code to complete. Check the results with assertions afterwards.
        
        let fileURL = Bundle.main.url(forResource: "boatstate_20230123_130812_0095", withExtension: "bin")
        let content = try Data(contentsOf: fileURL!)
        let res = try (content as NSData).decompressed(using: .zlib)
        do {
            let decoder = JSONDecoder()
            let jsonData = try decoder.decode([BoatState.TrkPt].self, from: res as Data)
            print(jsonData.count)
        } catch {
            print("error:\(error)")
        }
        // let writeURL =
    }
    
    func testFile() {
        let fileManager = FileManager.default
        let directory = fileManager.temporaryDirectory
        let filename = UUID().uuidString
        let fileURL = directory.appendingPathComponent(filename)
        do {
            try fileManager.createDirectory(at: fileURL.deletingLastPathComponent(), withIntermediateDirectories: true)
            fileManager.createFile(atPath: fileURL.path, contents: "test".data(using: .utf8))
            print(fileURL.path)
        } catch {
            XCTFail("Error while making temp dir: \(error)")
        }
    }
    

    
    func testPerformanceExample() throws {
        // This is an example of a performance test case.
        self.measure {
            // Put the code you want to measure the time of here.
        }
    }

}
