//
//  LIneView.swift
//  BoatStart
//
//  Created by Paul Viola on 11/27/22.
//

import SwiftUI

struct LineView: View {
  
    @ObservedObject var boatState: BoatState
    
    struct Geometry {
        var boatSize: CGFloat = 0.0
//        var size: CGSize
//        var pipSize: CGFloat
//        var position: Mapping.vec
    }
    
    func pf (_ value: Double?, _ precision: Int = 2, ifMissing: Double = -1.0) -> String  {
        let val = value ?? ifMissing
        return String(format: "%.\(precision)F", val)
    }
    
    var geometry = Geometry()
    
    func makeTap(_ message: String) -> some Gesture {
        return  TapGesture(count: 1) .onEnded { print(message) }
    }
    
    var body: some View {
        GeometryReader { metrics in
            let size = metrics.size
            let boatSize = min(size.width, size.height) / 5.0  // How big are the boats on the screen
            let pipSize = boatSize/4.0
            let scaledDistance = boatState.distanceToLine() / 1.0  // Scale on the screen for distance to the line
            let along = boatState.positionAlongLine()
            let position = Vector((1.0-along) * size.width, scaledDistance * boatSize + boatSize/2.0)
//            let position = Mapping.Vector((1.0-0.5) * size.width, 3.0 * boatSize + boatSize/2.0)
            ZStack {
                Canvas { context, size in
                    var pos = boatSize/2
                    var color = Color.red
                    while pos < size.height {
                        context.stroke(
                            MidLine()
                                .stroke(style: StrokeStyle(lineWidth: 1, dash: [10, 20]))
                                .path(in: CGRect(x: 0, y: pos, width: size.width, height: 1)),
                            with: .color(color))
                        pos = pos + boatSize
                        color = Color.blue
                    }
                }
                HStack {
                    Rectangle()
                        .stroke(.red)
                        .contentShape(Rectangle())
                        .gesture(makeTap("boat edge tapped"))
                }
                .frame(width: boatSize, height: size.height)
                .position(x: size.width - boatSize/2, y: size.height/2)
                HStack {
                    Rectangle()
                        .stroke(.red)
                        .contentShape(Rectangle())
                        .gesture(makeTap("pin edge tapped"))
                }
                .frame(width: boatSize, height: size.height)
                .position(x: boatSize/2, y: size.height/2)
                HStack {
                    BoatShape()
                        .fill(.blue)
                        .contentShape(Rectangle())
                        .onTapGesture {
                            boatState.dropBoat()
                            print("boat tapped")
                        }
                }
                .frame(width: boatSize, height: boatSize)
                .position(x: size.width - boatSize/2, y: boatSize/2)
                HStack {
                    Circle()
                        .fill(.blue)
                        .contentShape(Rectangle())
                        .onTapGesture {
                            boatState.dropPin()
                            print("pin tapped")
                        }
                }
                .frame(width: boatSize, height: boatSize)
                .position(x: boatSize/2, y: boatSize/2)
                HStack {
                    Circle()
                        .fill(.red)
                        .contentShape(Rectangle())
                }
                .frame(width: pipSize, height: pipSize)
                .position(x: position.x + pipSize/2.0, y: position.y + pipSize/2.0)
                VStack {
                    Text("\(pf(along, 2)), \(pf(scaledDistance, 2))")
                    Text("\(pf(position.x, 2)), \(pf(position.y, 2))")
                }
            }
        }
        .ignoresSafeArea()
    }
}

struct LineView_Previews: PreviewProvider {
    static var previews: some View {
        LineView(boatState: BoatState())
            .previewInterfaceOrientation(.landscapeLeft)
    }
}
