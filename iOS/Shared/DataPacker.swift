//
//  DataPacker.swift
//  Basic Chat MVC
//
//  Created by Paul Viola on 11/7/22.
//

import Foundation

/// General extension that allows you to pack and unpack typed data to and from bytes.
extension Data {

    enum DataError: Error {
      case notEnoughBytes
    }
    
    /// Convert a single item to bytes
    init<T>(from value: T) {
        self = Swift.withUnsafeBytes(of: value) { Data($0) }
    }
    
    /// Convert an array of typed items
    init<T>(fromArray values: [T]) {
        self = values.withUnsafeBytes { Data($0) }
    }

    /// Given a reference to a var, pull the bytes necessary to deserialize.
    func pull<T>(_ value: inout T) throws
    where T: ExpressibleByIntegerLiteral {
        guard count >= MemoryLayout.size(ofValue: value) else { throw DataError.notEnoughBytes }
        _ = Swift.withUnsafeMutableBytes(of: &value, { copyBytes(to: $0)} )
    }
    
    func to<T>(type: T.Type) -> T? where T: ExpressibleByIntegerLiteral {
        var value: T = 0
        guard count >= MemoryLayout.size(ofValue: value) else { return nil }
        _ = Swift.withUnsafeMutableBytes(of: &value, { copyBytes(to: $0)} )
        return value
    }

    func toArray<T>(type: T.Type) -> [T] where T: ExpressibleByIntegerLiteral {
        var array = Array<T>(repeating: 0, count: self.count/MemoryLayout<T>.stride)
        _ = array.withUnsafeMutableBytes { copyBytes(to: $0) }
        return array
    }
}

class TestDataPacker {

    static func testDouble() {
        let value = Double(42.13)
        let data = Data(from: value)
        print(data as NSData) // <713d0ad7 a3104540>
        
        if let roundtrip = data.to(type: Double.self) {
            print(roundtrip) // 42.13
        } else {
            print("not enough data")
        }
    }
    
    static func testArray() {
        
        let value: [Int16] = [1, Int16.max, Int16.min]
        let data = Data(fromArray: value)
        print(data as NSData) // <0100ff7f 0080>
        
        let roundtrip = data.toArray(type: Int16.self)
        print(roundtrip) // [1, 32767, -32768]
    }
    
    static func testInOut() {
        
        let value: [UInt16] = [0, UInt16.max, UInt16.max]
        let data = Data(fromArray: value)
        print(data as NSData) // <0100ff7f 0080>
        
        let roundtrip = data.toArray(type: UInt16.self)
        print(roundtrip) // [0, 65535, 65535]
        
        var res : UInt16 = 0
        
        do {
            try data.pull(&res)
            print(res)
            try data.dropFirst(2).pull(&res)
            print(res)
        } catch {
            print("WARNING \(error)")
        }
    }
}
