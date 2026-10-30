package xiaozhi.modules.connection.controller;

import java.util.Map;
import lombok.RequiredArgsConstructor;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;
import xiaozhi.modules.connection.service.DeviceGrantService;

@RestController
@RequiredArgsConstructor
public class NativeOtaController {
    private final DeviceGrantService grants;
    @PostMapping("/ota/native/{token}")
    public ResponseEntity<?> ota(@PathVariable String token,
            @RequestHeader(value="Device-Id",required=false) String mac,
            @RequestHeader(value="Client-Id",required=false) String client) {
        try {return ResponseEntity.ok().header("Cache-Control","no-store").body(grants.ota(token,mac,client));}
        catch(xiaozhi.common.exception.RenException e) {
            return ResponseEntity.status(403).header("Cache-Control","no-store").body(Map.of("error","device_access_denied"));
        }
    }
}
