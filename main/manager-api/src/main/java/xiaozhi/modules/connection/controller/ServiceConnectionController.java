package xiaozhi.modules.connection.controller;

import java.util.List;
import java.util.Map;
import jakarta.validation.Valid;
import jakarta.validation.constraints.Size;
import org.apache.shiro.authz.annotation.RequiresPermissions;
import org.springframework.web.bind.annotation.*;
import lombok.AllArgsConstructor;
import xiaozhi.common.utils.Result;
import xiaozhi.modules.connection.dto.ConnectionBody;
import xiaozhi.modules.connection.entity.ServiceConnectionEntity;
import xiaozhi.modules.connection.service.ServiceConnectionService;
import xiaozhi.modules.security.user.SecurityUser;

@RestController
@RequestMapping("service-connections")
@RequiresPermissions("sys:role:superAdmin")
@AllArgsConstructor
public class ServiceConnectionController {
    private final ServiceConnectionService service;
    private final xiaozhi.modules.connection.service.DeviceGrantService grants;
    public record GrantRequest(@Size(max=128) String clientId, @Size(max=1024) String originalOtaUrl) {}
    @GetMapping("/devices/{id}/access")
    public Result<Map<String,Object>> access(@PathVariable String id,jakarta.servlet.http.HttpServletResponse response) {
        response.setHeader("Cache-Control","no-store");
        return new Result<Map<String,Object>>().ok(grants.status(id,SecurityUser.getUserId()));
    }
    @PostMapping("/devices/{id}/access")
    public Result<Map<String,Object>> provision(@PathVariable String id,@Valid @RequestBody GrantRequest body,jakarta.servlet.http.HttpServletResponse response) {
        response.setHeader("Cache-Control","no-store");
        return new Result<Map<String,Object>>().ok(grants.provision(id,SecurityUser.getUserId(),body.clientId()==null?"":body.clientId(),body.originalOtaUrl()));
    }
    @PostMapping("/devices/{id}/access/renew")
    public Result<Void> renew(@PathVariable String id,jakarta.servlet.http.HttpServletResponse response) {
        response.setHeader("Cache-Control","no-store");grants.renew(id,SecurityUser.getUserId());return new Result<>();
    }
    @PostMapping("/devices/{id}/access/revoke")
    public Result<Void> revoke(@PathVariable String id,jakarta.servlet.http.HttpServletResponse response) {
        response.setHeader("Cache-Control","no-store");grants.revoke(id,SecurityUser.getUserId());return new Result<>();
    }
    private final xiaozhi.modules.connection.service.DeviceAccessService deviceAccess;
    public record NativeTokenRequest(@jakarta.validation.constraints.NotBlank
        @jakarta.validation.constraints.Pattern(regexp="[A-Za-z0-9:_-]{1,128}") String clientId) {}

    @GetMapping("/devices/{id}/readiness")
    public Result<Map<String,Object>> readiness(@PathVariable String id, jakarta.servlet.http.HttpServletResponse response) {
        response.setHeader("Cache-Control", "no-store");
        return new Result<Map<String,Object>>().ok(deviceAccess.readiness(id,SecurityUser.getUserId()));
    }
    @PostMapping("/devices/{id}/native-token")
    public Result<Map<String,Object>> nativeToken(@PathVariable String id, @Valid @RequestBody NativeTokenRequest body,
            jakarta.servlet.http.HttpServletResponse response) {
        response.setHeader("Cache-Control", "no-store");
        return new Result<Map<String,Object>>().ok(deviceAccess.issue(id,body.clientId(),SecurityUser.getUserId()));
    }

    private final xiaozhi.modules.connection.service.AccountAuthorizationService authorization;

    @GetMapping("/{id}/account")
    public Result<Map<String, Object>> account(@PathVariable String id, jakarta.servlet.http.HttpServletResponse response) {
        response.setHeader("Cache-Control", "no-store");
        return new Result<Map<String, Object>>().ok(authorization.account(id, SecurityUser.getUserId(), "status"));
    }
    @PostMapping("/{id}/account/login")
    public Result<Map<String, Object>> login(@PathVariable String id, jakarta.servlet.http.HttpServletResponse response) {
        response.setHeader("Cache-Control", "no-store");
        return new Result<Map<String, Object>>().ok(authorization.account(id, SecurityUser.getUserId(), "start"));
    }
    @PostMapping("/{id}/account/cancel")
    public Result<Map<String, Object>> cancel(@PathVariable String id, jakarta.servlet.http.HttpServletResponse response) {
        response.setHeader("Cache-Control", "no-store");
        return new Result<Map<String, Object>>().ok(authorization.account(id, SecurityUser.getUserId(), "cancel"));
    }
    @PostMapping("/{id}/account/refresh")
    public Result<Map<String,Object>> refreshAccount(@PathVariable String id,jakarta.servlet.http.HttpServletResponse response) {
        response.setHeader("Cache-Control","no-store");
        return new Result<Map<String,Object>>().ok(authorization.account(id,SecurityUser.getUserId(),"refresh"));
    }
    @PostMapping("/{id}/account/reauthorize")
    public Result<Map<String,Object>> reauthorizeAccount(@PathVariable String id,jakarta.servlet.http.HttpServletResponse response) {
        response.setHeader("Cache-Control","no-store");
        return new Result<Map<String,Object>>().ok(authorization.account(id,SecurityUser.getUserId(),"reauthorize"));
    }

    public record Selection(@Size(max = 64) String connectionId) {}

    @GetMapping("/devices")
    public Result<List<Map<String, String>>> devices() {
        return new Result<List<Map<String, String>>>().ok(service.devices(SecurityUser.getUserId()));
    }

    @GetMapping
    public Result<List<ServiceConnectionEntity>> list() {
        return new Result<List<ServiceConnectionEntity>>().ok(service.list(SecurityUser.getUserId()));
    }
    @PostMapping
    public Result<ServiceConnectionEntity> create(@Valid @RequestBody ConnectionBody body) {
        return new Result<ServiceConnectionEntity>().ok(service.save(null, body, SecurityUser.getUserId()));
    }
    @PutMapping("/{id}")
    public Result<ServiceConnectionEntity> update(@PathVariable String id, @Valid @RequestBody ConnectionBody body) {
        return new Result<ServiceConnectionEntity>().ok(service.save(id, body, SecurityUser.getUserId()));
    }
    @GetMapping("/devices/{id}")
    public Result<Map<String, String>> selection(@PathVariable String id) {
        return new Result<Map<String, String>>().ok(service.selection(id, SecurityUser.getUserId()));
    }
    @PutMapping("/devices/{id}")
    public Result<Void> select(@PathVariable String id, @Valid @RequestBody Selection body) {
        service.select(id, body.connectionId(), SecurityUser.getUserId());
        return new Result<>();
    }
}
