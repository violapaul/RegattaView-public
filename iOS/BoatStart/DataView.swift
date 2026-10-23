//
//  DataView.swift
//  BoatStart
//
//  Created by Paul Viola on 11/20/22.
//

import Foundation
import SwiftUI

struct DataView: View {
    
    @ObservedObject var boatState: BoatManager
    @State private var rotation: Angle = .zero
    
    func raceBoxConnect () {
        if boatState.hasRaceBox() {
            boatState.raceBoxConnect()
        }
    }
    
    func dropPin() {
        print("Drop pin!!")
        boatState.pingPin()
    }
    
    func dropBoat() {
        print("Drop Boat!!")
        boatState.pingBoat()
    }
    
    func saveTrack() {
        boatState.saveTrack()
    }
    
    var horizontalAccuracy: String {
        return "\(boatState.state.accuracy)"
    }
    
    var userLatitude: String {
        return "\(boatState.state.LLA.lat)"
    }
    
    var userLongitude: String {
        return "\(boatState.state.LLA.lon)"
    }
    
    var userSOG: String {
        return "\(boatState.state.sog)"
    }
    
    var userCOG: String {
        return "\(boatState.state.cog)"
    }
    
    var userCompass: String {
        return "\(boatState.state.hdg)"
    }
    
    func getDocumentsDirectory() -> URL {
        // find all possible documents directories for this user
        let paths = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)
        for p in paths {
            print(p)
        }
        
        // just send back the first one, which ought to be the only one
        return paths[0]
    }
    
    var body: some View {
        let pin = boatState.state.pinNED
        let boat = boatState.state.boatNED
        let current = boatState.state.ned
        VStack {
            
            if boatState.hasRaceBox() {
                Button(action: raceBoxConnect) {
                    Text("Connect to Racebox")
                }
                Text(boatState.status())
            }
            Button(action: saveTrack) {
                Text("Save Track!")
            }
            
            Button(action: dropPin) {
                Text("Drop Pin!")
            }
            Text("Pin \(Ux.pf(pin.x)), \(Ux.pf(pin.y))")
            
            Button(action: dropBoat) {
                Text("Drop Boat!")
            }

            Text("Boat \(Ux.pf(boat.x)), \(Ux.pf(boat.y))")
            Text("Loc \(current.x), \(current.y)")
            Text("Distance \(boatState.distanceToLine())")
            Text("Position \(boatState.distanceAlongLine())")
            Text("horizontal accuracy \(horizontalAccuracy)")
            
//                HStack {
//                    Text("lat: \(userLatitude)")
//                    Text("lon: \(userLongitude)")
//                }
//                Text("SOG: \(userSOG)")
//                Text("COG: \(userCOG)")
//                Text("Compass: \(userCompass)")
        }.buttonStyle(.bordered)
    }
}

struct DataViewView_Previews: PreviewProvider {
    static var previews: some View {
        DataView(boatState: BoatManager())
            .previewInterfaceOrientation(.landscapeLeft)
    }
}
