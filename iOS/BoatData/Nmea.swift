//
//  Nmea.swift
//  Code parses NMEA 0183 sentences
//
//  Created by Paul Viola on 10/23/23.
//

import Foundation

// Read https://gpsd.gitlab.io/gpsd/NMEA.html for additional information



protocol NMEASentenceParsable {
    static func parse(sentence: String) -> Self?
}

struct Nmea {
    static func calculateChecksum(sentence: String) -> String {
        var checksum: UInt8 = 0
        for char in sentence.utf8 {
            checksum ^= char
        }
        return String(format: "%02X", checksum)
    }

    /// Strip the checksum from a sentence.
    static func stripForChecksum(sentence: String) -> String? {
        guard let startDelimiter = sentence.first, (startDelimiter == "$" || startDelimiter == "!"),
              let asteriskIndex = sentence.lastIndex(of: "*") else {
            return nil
        }
        
        let range = sentence.index(after: sentence.startIndex)..<asteriskIndex
        return String(sentence[range])
    }
    
    
    static func validateChecksum(sentence: String) -> Bool {
        // Identify the start delimiter and ensure it's either "$" or "!"
        guard let startDelimiter = sentence.first, (startDelimiter == "$" || startDelimiter == "!"),
              let asteriskIndex = sentence.lastIndex(of: "*") else {
            return false
        }
        
        let range = sentence.index(after: sentence.startIndex)..<asteriskIndex
        let contentForChecksum = String(sentence[range])
        
        // Extract the provided checksum from the message
        let checksumStartIndex = sentence.index(after: asteriskIndex)
        let providedChecksum = String(sentence[checksumStartIndex...])
        
        // Calculate the checksum from the message content
        let computedChecksum = calculateChecksum(sentence: contentForChecksum)
        
        return computedChecksum == providedChecksum
    }
    
    // The messages we will parse are defined below.
    
    // Critical values includes SOG and COG.  Most of the rest are either undefined or useless
    struct RMC : NMEASentenceParsable {
        var time: String?
        var status: String?
        var latitude: Double?
        var latitudeDirection: String?
        var longitude: Double?
        var longitudeDirection: String?
        var speedOverGround: Double?  // SOG
        var trackMadeGood: Double?    // COG strange name
        var date: String?
        var magneticVariation: Double?
        var magneticDirection: String?
        var faaModeIndicator: String?
        var navStatus: String?
        
        static func parse(sentence: String) -> RMC? {
            let components = sentence.components(separatedBy: ",")
            guard components.count >= 12 else {
                logWarn("Invalid RMC sentence.")
                return nil
            }
//
//            1         2 3       4 5        6  7   8   9    10 11
//            |         | |       | |        |  |   |   |    |  |
//     $--RMC,hhmmss.ss,A,ddmm.mm,a,dddmm.mm,a,x.x,x.x,xxxx,x.x,a*hh<CR><LF>
//    NMEA 2.3:
//     $--RMC,hhmmss.ss,A,ddmm.mm,a,dddmm.mm,a,x.x,x.x,xxxx,x.x,a,m*hh<CR><LF>
//    NMEA 4.1:
//     $--RMC,hhmmss.ss,A,ddmm.mm,a,dddmm.mm,a,x.x,x.x,xxxx,x.x,a,m,s*hh<CR><LF>
            
            var data = RMC()
            data.time = components[1]
            data.status = components[2]
            data.latitude = Double(components[3])
            data.latitudeDirection = components[4]
            data.longitude = Double(components[5])
            data.longitudeDirection = components[6]
            data.speedOverGround = Double(components[7])
            data.trackMadeGood = Double(components[8])
            data.date = components[9]
            data.magneticVariation = Double(components[10])
            data.magneticDirection = components[11]
            
            if components.count >= 13 {
                data.faaModeIndicator = components[12]
            }
            
            if components.count >= 14 {
                data.navStatus = components[13]
            }
            
            return data
        }
    }
    
    // Critical values includes AWA and AWS.
    struct MWV : NMEASentenceParsable {
        var windAngle: Double?  // AWA and TWA
        var reference: String?  // 'R' or Relative is AWA/AWS. 'T' or True is TWA/TWS
        var windSpeed: Double?  // AWS and TWS
        var windSpeedUnits: String?
        var status: String?
        
        static func parse(sentence: String) -> MWV? {
            
            let components = sentence.components(separatedBy: ",")
            guard components.count >= 6 else {
                logWarn("Invalid MWV sentence.")
                return nil
            }
            
//            1   2 3   4 5
//            |   | |   | |
//     $--MWV,x.x,a,x.x,a*hh<CR><LF>
            
            var data = MWV()
            data.windAngle = Double(components[1])
            data.reference = components[2]
            data.windSpeed = Double(components[3])
            data.windSpeedUnits = components[4]
            data.status = components[5]
            
            return data
        }
    }

     struct MWD : NMEASentenceParsable {
         var windDirectionTrue: Double?         // TWD
         var windDirectionMagnetic: Double?
         var windSpeedKnots: Double?            // TWS.  Typically this is identical to value sent in MWV
         var windSpeedMetersPerSecond: Double?
         
         static func parse(sentence: String) -> MWD? {
             let components = sentence.components(separatedBy: ",")
             guard components.count >= 8 else {  // Making sure we have enough components
                 logWarn("Invalid MWD sentence.")
                 return nil
             }
             
             var data = MWD()
             data.windDirectionTrue = Double(components[1])
             data.windDirectionMagnetic = Double(components[3])
             data.windSpeedKnots = Double(components[5])
             data.windSpeedMetersPerSecond = Double(components[7])
             
             return data
         }
     }
    
    // Critical values includes HDG, both magnetic and true and SPD
    struct VHW : NMEASentenceParsable {
        var headingTrue: Double?       // HDG Everything is True in this app.
        var headingMagnetic: Double?
        var speedKnots: Double?        // SPD, we'll use this, but convert back to m/s
        var speedKmHr: Double?
        
        static func parse(sentence: String) -> VHW? {
            let components = sentence.components(separatedBy: ",")
            guard components.count >= 9 else {
                logWarn("Invalid VHW sentence.")
                return nil
            }
            
            var data = VHW()
            data.headingTrue = Double(components[1])
            data.headingMagnetic = Double(components[3])
            data.speedKnots = Double(components[5])
            data.speedKmHr = Double(components[7])
            
            return data
        }
    }
 
    
    static func dispatchToParser(sentence: String) -> NMEASentenceParsable? {
        if !Nmea.validateChecksum(sentence: sentence) {
            logWarn("Invalid checksum.")
            return nil
        }
        guard sentence.count > 6 else { return nil }
        
        let significantChars = String(sentence[sentence.index(sentence.startIndex, offsetBy: 3)...].prefix(3))
        switch significantChars {
        case "RMC":
            return RMC.parse(sentence: sentence)
        case "MWV":
            return MWV.parse(sentence: sentence)
        case "MWD":
            return MWD.parse(sentence: sentence)
        case "VHW":
            return VHW.parse(sentence: sentence)
        default:
            return nil
        }
    }
}


