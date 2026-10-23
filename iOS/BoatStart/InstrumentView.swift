//
//  InstrumentView.swift
//  BoatStart
//
//  Created by Paul Viola on 12/5/22.
//

import SwiftUI

struct InstrumentView: View {
    
    @ObservedObject var boatState: BoatState
    @ObservedObject var timeManager: TimeManager

    func pf (_ value: Double?, _ precision: Int = 2, ifMissing: Double = -1.0) -> String  {
        let val = value ?? ifMissing
        return String(format: "%.\(precision)F", val)
    }
    
    func pf (_ value: Int?, _ precision: Int = 2, ifMissing: Int = -1) -> String  {
        let val = value ?? ifMissing
        return String(format: "%.\(precision)d", val)
    }
    
    var body: some View {
        let distanceAtStart = boatState.state.distanceToLine - boatState.state.speedToLine * timeManager.timeToStart
        let timeToBurn = boatState.state.timeToLine - timeManager.timeToStart
        
        HStack (spacing: 50.0) {
            Grid {
                GridRow {
                    Text("Lat")
                        .gridColumnAlignment(.leading)
                    Text(pf(boatState.state.LLA.lat, 5))
                        .gridColumnAlignment(.trailing)
                }
                GridRow {
                    Text("Lon")
                    Text(pf(boatState.state.LLA.lon, 5))
                }
                GridRow {
                    Text("SOG")
                    Text(pf(boatState.state.sog, 1))
                }
                GridRow {
                    Text("COG")
                    Text(pf(boatState.state.cog, 1))
                }
                GridRow {
                    Text("HDG")
                    Text(pf(boatState.state.hdg, 1))
                }
                GridRow {
                    Text("RHDG")
                    Text("\(pf(boatState.state.rawHdg, 1)) \(pf(boatState.compassOffsetFilter.offset))")
                }
                GridRow {
                    Text("H Acc")
                    Text(pf(boatState.state.accuracy, 2))
                }
                GridRow {
                    Text("F")
                    Text("\(pf(boatState.state.force.x, 2)) \(pf(boatState.state.force.y, 2)) \(pf(boatState.state.force.z, 2))")
                }
            }
            Grid {
                GridRow {
                    Text("Loc")
                        .gridColumnAlignment(.leading)
                    let loc = boatState.state.ned
                    Text("\(pf(loc.x, 1)) \(pf(loc.y, 1))")
                        .gridColumnAlignment(.trailing)
                }
                GridRow {
                    Text("Pin")
                    let loc = boatState.state.pinNED
                    Text("\(pf(loc.x, 1)) \(pf(loc.y, 1))")
                }
                GridRow {
                    Text("Boat")
                    let loc = boatState.state.boatNED
                    Text("\(pf(loc.x, 1)) \(pf(loc.y, 1))")
                }
                
                GridRow {
                    Text("Dist")
                    Text(pf(boatState.distanceToLine(), 1))
                }
                GridRow {
                    Text("Pos")
                    Text(pf(boatState.distanceAlongLine(), 1))
                }
            }
            Grid{
                GridRow {
                    Text("Dist2Line")
                        .gridColumnAlignment(.leading)
                    Text(pf(boatState.distanceToLine(), 1))
                        .gridColumnAlignment(.trailing)
                }
                GridRow {
                    Text("Speed2Line")
                    Text(pf(boatState.state.speedToLine, 2))
                }
                GridRow {
                    Text("Time2Line")
                    Text(pf(boatState.state.timeToLine, 1))
                }
                GridRow {
                    Text("Time2Start")
                    Text(pf(timeManager.timeToStart, 2))
                }
                GridRow {
                    Text("Time2Burn")
                    Text(pf(timeToBurn, 2))
                }
                GridRow {
                    Text("DistAtStart")
                    Text(pf(distanceAtStart, 2))
                }
                GridRow {
                    Text("AfterHDG")
                    Text(pf(boatState.state.afterTackHdg, 1))
                }
                GridRow {
                    Text("State")
                    Text(boatState.tackDetector.state.rawValue)
                }
            }
        }
        .font(Font.system(size: 25).monospacedDigit())
    }
}

struct InstrumentView_Previews: PreviewProvider {
    static var previews: some View {
        InstrumentView(boatState: BoatState(), timeManager: TimeManager())
            .previewInterfaceOrientation(.landscapeLeft)
    }
}
